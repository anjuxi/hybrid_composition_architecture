import argparse;
import json;
import os;
import sys;
import urllib.error;
import urllib.request;
from typing import Annotated, TypedDict;

from langgraph.graph import END, START, StateGraph;

# 不带 --task 时跑这个。
V_示例任务 = "为电商 App 设计一个限时秒杀功能，要求扛住高并发、满足安全合规，并给出可回滚的上线计划。";

# 走 OpenAI 兼容协议，换服务商改环境变量就行。
V_接口地址 = os.environ.get("OPENAI_BASE_URL", "https://api.chatanywhere.tech/v1").rstrip("/");
V_模型名称 = os.environ.get("OPENAI_MODEL", "gpt-4o-mini");
V_接口密钥 = os.environ.get("OPENAI_API_KEY", "");

# 最多讨论三轮，还谈不拢就交给人。
V_最大评审轮次 = 3;

# 三个业务单元。结构在设计期定死，运行时不改拓扑。
V_单元配置 = [
    {
        "key": "product",
        "name": "产品单元",
        "goal": "澄清用户场景、需求边界与验收口径",
        "steps": ["需求拆解", "用户场景梳理", "验收标准定义"],
    },
    {
        "key": "tech",
        "name": "技术单元",
        "goal": "确定架构方案、接口边界与技术风险",
        "steps": ["架构设计", "接口定义", "技术风险评估"],
    },
    {
        "key": "delivery",
        "name": "交付单元",
        "goal": "形成上线清单、灰度方案与回滚预案",
        "steps": ["上线清单", "灰度方案", "回滚预案"],
    },
];

# 圆桌四类视角，各自只看自己那摊。
V_专家配置 = [
    {"name": "产品专家", "focus": "用户价值与需求边界"},
    {"name": "架构专家", "focus": "系统结构、性能与可扩展性"},
    {"name": "安全专家", "focus": "安全、合规与数据保护"},
    {"name": "测试专家", "focus": "质量保障与可测性"},
];

# 离线模式下的交付要点，没密钥也能跑出像样的产物。
V_步骤要点 = {
    "需求拆解": "拆出功能点、边界条件和依赖项，形成可执行的任务清单。",
    "用户场景梳理": "梳理核心用户路径和异常路径，标注关键体验节点。",
    "验收标准定义": "定义可量化的验收指标，明确什么算做完、什么算达标。",
    "架构设计": "给出分层结构、并发模型和扩展点，说明关键取舍。",
    "接口定义": "约定输入输出、错误码和幂等规则，减少联调摩擦。",
    "技术风险评估": "列出性能、一致性和依赖风险，并给出缓解措施。",
    "上线清单": "整理发布前检查项，逐条确认责任人和完成状态。",
    "灰度方案": "设计分批放量节奏和观察指标，出问题能第一时间发现。",
    "回滚预案": "明确回滚触发条件、操作步骤和数据补偿方案。",
};

# 离线发言模板，按角色区分口吻。
V_离线专家模板 = {
    "产品专家": "从用户价值出发，《{task}》在{unit}阶段要先锁住核心场景和验收口径，避免需求范围失控。",
    "架构专家": "从架构角度看，{unit}需要把并发模型、依赖边界和扩展点讲清楚，给后续实现留出余量。",
    "安全专家": "安全合规不能后置，{unit}要把权限控制、数据流向和审计要求写进方案。",
    "测试专家": "建议在{unit}阶段就定义可测指标，异常路径和回滚验证都要覆盖到。",
};


def merge_blackboard(old_value: dict, new_value: dict) -> dict:
    # 子图只回传增量，这里递归合并。直接 update 会把别的单元写进黑板的键整个冲掉。
    merged = dict(old_value or {});
    for key, value in (new_value or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_blackboard(merged[key], value);
        else:
            merged[key] = value;
    return merged;


class HybridState(TypedDict, total=False):
    # 所有子图共用这一份状态，黑板单独做递归合并。
    task: str;
    blackboard: Annotated[dict, merge_blackboard];  # 计划、评审、风险、产物都在这。
    round_index: int;  # 当前单元的评审轮次。
    round_opinions: list;  # 历史发言，主持人按 round 字段取本轮。
    review_verdict: str;  # 通过 / 驳回 / 升级。
    review_reason: str;
    pipeline_artifacts: list;
    pipeline_error: str;  # 空串表示没出错。
    unit_records: list;  # 已完成单元的交付记录。
    escalations: list;  # 升级原因和恢复目标。
    resume_target: str;
    human_decision: str;
    terminated: bool;
    final_report: str;


def call_llm(system_prompt: str, user_prompt: str) -> str:
    # 标准库 urllib 够用了，省得为一次 HTTP 调用装 SDK。timeout 给足，长文生成本来就慢。
    payload = {
        "model": V_模型名称,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
    };
    request = urllib.request.Request(
        f"{V_接口地址}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {V_接口密钥}",
        },
        method="POST",
    );
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.loads(response.read().decode("utf-8"));
    return body["choices"][0]["message"]["content"].strip();


def parse_json_block(text: str) -> dict:
    # 模型有时候会在 JSON 外面加解释或代码围栏，取首尾花括号之间的内容再试。
    start = text.find("{");
    end = text.rfind("}");
    if start == -1 or end == -1 or end <= start:
        return {};
    try:
        return json.loads(text[start:end + 1]);
    except json.JSONDecodeError:
        return {};


def merge_unique(old_items: list, new_items: list) -> list:
    # 风险可能被多个专家重复提，按原顺序去重。
    merged = list(old_items or []);
    for item in new_items or []:
        if item not in merged:
            merged.append(item);
    return merged;


def offline_expert_reply(expert: dict, task: str, unit: str, round_index: int, force_reject: bool) -> dict:
    # 离线专家意见。技术单元第一轮故意让安全专家驳回，第二轮放行，用来演示多轮讨论。
    # force_reject 打开时一律驳回，专门给升级链路做演练。
    if force_reject:
        return {
            "opinion": f"{unit}的方案在核心风险上始终没有闭环，我这边继续持反对意见。",
            "verdict": "驳回",
            "risks": ["指定演练单元持续不通过，用于验证升级链路。"],
        };
    if unit == "技术单元" and expert["name"] == "安全专家" and round_index == 1:
        return {
            "opinion": f"《{task}》的流量峰值和安全边界没讲透，{unit}方案现在还不能放行。",
            "verdict": "驳回",
            "risks": ["关键链路缺少限流与降级策略。", "数据一致性缺少幂等校验。"],
        };
    template = V_离线专家模板.get(expert["name"], "{unit}还需要补充细节。");
    return {
        "opinion": template.format(task=task, unit=unit),
        "verdict": "通过",
        "risks": [],
    };


def expert_reply(expert: dict, task: str, unit: str, goal: str, round_index: int, history: list, online_mode: bool, force_reject: bool) -> dict:
    # 在线优先。模型返回格式不对或者请求失败，都退回离线，流程不能因为模型抽风中断。
    if online_mode:
        system_prompt = (
            f"你是企业多智能体系统中的{expert['name']}，专注{expert['focus']}。"
            "你正在参加业务单元的圆桌评审，请给出专业、具体、可执行的意见。"
            "只输出 JSON，不要输出其他内容，格式为："
            '{"意见": "一段中文意见", "结论": "通过或驳回", "风险": ["风险点"]}。'
        );
        history_text = "；".join(f"第{item['round']}轮{item['expert']}：{item['opinion']}" for item in history[-8:]);
        user_prompt = (
            f"整体任务：{task}\n业务单元：{unit}\n单元目标：{goal}\n当前轮次：{round_index}\n"
            f"历史发言：{history_text or '暂无'}\n请给出本轮意见。"
        );
        try:
            content = call_llm(system_prompt, user_prompt);
            data = parse_json_block(content);
            verdict = str(data.get("结论", "")).strip();
            if data.get("意见") and verdict in ("通过", "驳回"):
                risks = data.get("风险") or [];
                risk_list = [str(item) for item in risks] if isinstance(risks, list) else [str(risks)];
                return {"opinion": str(data["意见"]), "verdict": verdict, "risks": risk_list};
            print(f"[提示] {expert['name']}的大模型回复不符合约定格式，已回退离线大脑！");
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError) as error:
            print(f"[提示] 大模型调用失败（{error}），已回退离线大脑！");
    return offline_expert_reply(expert, task, unit, round_index, force_reject);


def pipeline_step_content(unit: str, step: str, goal: str, task: str, state: HybridState, online_mode: bool) -> str:
    # 在线让模型写，离线按要点拼。评审阶段登记的风险带上，产物至少能呼应结论。
    if online_mode:
        system_prompt = (
            "你是企业多智能体系统中的执行工程师，负责把评审通过的方案落成交付物。"
            "请直接输出正文，不要客套话，不要 Markdown 代码块。"
        );
        blackboard = state.get("blackboard", {});
        risk_text = "；".join(blackboard.get("risks", [])) or "暂无";
        user_prompt = (
            f"整体任务：{task}\n业务单元：{unit}\n单元目标：{goal}\n当前步骤：{step}\n"
            f"已登记风险：{risk_text}\n请输出本步骤的交付内容，要求具体可用。"
        );
        try:
            content = call_llm(system_prompt, user_prompt);
            if content:
                return content;
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError) as error:
            print(f"[提示] 流水线步骤调用大模型失败（{error}），已回退离线模板！");
    points = V_步骤要点.get(step, f"完成{step}，支撑单元目标。");
    return f"{points}本步骤服务于{unit}目标：{goal}。";


def make_expert_node(unit: dict, expert: dict, online_mode: bool, force_reject: bool):
    # 一个专家一个节点，只记意见，结论统一交给主持人。
    unit_name = unit["name"];

    def expert_node(state: HybridState) -> dict:
        round_index = state.get("round_index", 1);
        history = state.get("round_opinions", []);
        reply = expert_reply(
            expert, state["task"], unit_name, unit["goal"], round_index,
            history, online_mode, force_reject,
        );
        opinion = {
            "unit": unit_name,
            "round": round_index,
            "expert": expert["name"],
            "focus": expert["focus"],
            "opinion": reply["opinion"],
            "verdict": reply["verdict"],
            "risks": reply.get("risks", []),
        };
        print(f"[圆桌·{unit_name}] 第{round_index}轮 {expert['name']}给出结论：{reply['verdict']}！");
        return {"round_opinions": history + [opinion]};

    return expert_node;


def make_opening_node(unit: dict):
    # 新单元开场要清掉上一单元的轮次和意见，不然主持人会读到旧数据。
    unit_name = unit["name"];

    def opening_node(state: HybridState) -> dict:
        print(f"[圆桌·{unit_name}] 开始评审，单元目标：{unit['goal']}！");
        return {
            "round_index": 1,
            "round_opinions": [],
            "review_verdict": "",
            "review_reason": "",
        };

    return opening_node;


def make_host_node(unit: dict):
    # 汇总本轮意见出结论：全票通过就放行，连续三轮谈不拢就升级。
    unit_name = unit["name"];

    def host_node(state: HybridState) -> dict:
        round_index = state.get("round_index", 1);
        current_opinions = [
            item for item in state.get("round_opinions", [])
            if item.get("round") == round_index
        ];
        rejects = [item for item in current_opinions if item.get("verdict") == "驳回"];
        round_risks = [risk for item in current_opinions for risk in item.get("risks", [])];
        if not rejects:
            verdict = "通过";
            reason = f"{unit_name}第{round_index}轮全票通过";
        elif round_index >= V_最大评审轮次:
            verdict = "升级";
            reason = f"{unit_name}连续{V_最大评审轮次}轮没有谈拢，按规则向上升级";
        else:
            verdict = "驳回";
            reason = f"{unit_name}第{round_index}轮还有{len(rejects)}条驳回意见，进入下一轮";
        summary = {
            "unit": unit_name,
            "round": round_index,
            "verdict": verdict,
            "reason": reason,
            "risks": round_risks,
        };
        blackboard = {
            "reviews": {f"{unit_name}·第{round_index}轮": summary},
            "risks": merge_unique(state.get("blackboard", {}).get("risks", []), round_risks),
        };
        update = {
            "review_verdict": verdict,
            "review_reason": reason,
            "blackboard": blackboard,
        };
        # 升级时登记恢复目标，人工批准后直接回到该单元的流水线。
        if verdict == "升级":
            escalation = {
                "unit": unit_name,
                "kind": "评审卡壳",
                "reason": reason,
                "resume": f"{unit['key']}_pipeline",
            };
            update["escalations"] = state.get("escalations", []) + [escalation];
            update["resume_target"] = escalation["resume"];
        print(f"[圆桌·{unit_name}] 主持人定稿：{verdict}，{reason}！");
        return update;

    return host_node;


def roundtable_route(state: HybridState) -> str:
    # 驳回再开一轮，通过和升级都结束子图，后面怎么走由顶层决定。
    if state.get("review_verdict") == "通过":
        return "通过";
    if state.get("review_verdict") == "驳回":
        return "下一轮";
    return "升级";


def make_next_round_node():
    # 只做轮次加一，别的什么都不碰。
    def next_round_node(state: HybridState) -> dict:
        round_index = state.get("round_index", 1) + 1;
        print(f"[圆桌] 进入第{round_index}轮讨论！");
        return {"round_index": round_index};

    return next_round_node;


def build_roundtable_subgraph(unit: dict, online_mode: bool, force_reject: bool):
    # 专家按配置顺序串起来，主持人收口。路由绕回第一位专家就算新的一轮。
    graph = StateGraph(HybridState);
    graph.add_node("opening", make_opening_node(unit));
    expert_node_names = [];
    for expert in V_专家配置:
        node_name = f"expert_{expert['name']}";
        graph.add_node(node_name, make_expert_node(unit, expert, online_mode, force_reject));
        expert_node_names.append(node_name);
    graph.add_node("host", make_host_node(unit));
    graph.add_node("next_round", make_next_round_node());
    graph.add_edge(START, "opening");
    graph.add_edge("opening", expert_node_names[0]);
    for index in range(len(expert_node_names) - 1):
        graph.add_edge(expert_node_names[index], expert_node_names[index + 1]);
    graph.add_edge(expert_node_names[-1], "host");
    graph.add_conditional_edges(
        "host",
        roundtable_route,
        {"通过": END, "下一轮": "next_round", "升级": END},
    );
    graph.add_edge("next_round", expert_node_names[0]);
    return graph.compile();


def make_pipeline_step_node(unit: dict, step: str, step_index: int, step_count: int, online_mode: bool, next_node: str):
    # 第一步清空上一单元的产物，最后一步登记记录并写进黑板。
    unit_name = unit["name"];

    def step_node(state: HybridState) -> dict:
        if step_index == 1:
            artifacts = [];
            pipeline_error = "";
        else:
            artifacts = state.get("pipeline_artifacts", []);
            pipeline_error = state.get("pipeline_error", "");
        try:
            content = pipeline_step_content(
                unit_name, step, unit["goal"], state["task"], state, online_mode,
            );
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            content = f"{step}执行失败：{error}";
            pipeline_error = content;
        artifact = {"unit": unit_name, "step": step, "content": content};
        new_artifacts = artifacts + [artifact];
        update = {
            "pipeline_artifacts": new_artifacts,
            "pipeline_error": pipeline_error,
        };
        if step_index == step_count:
            content_list = [item["content"] for item in new_artifacts];
            record = {
                "unit": unit_name,
                "status": "交付完成" if not pipeline_error else "交付异常",
                "artifacts": content_list,
            };
            update["unit_records"] = state.get("unit_records", []) + [record];
            update["blackboard"] = {"artifacts": {unit_name: content_list}};
        if pipeline_error:
            escalation = {
                "unit": unit_name,
                "kind": "执行异常",
                "reason": pipeline_error,
                "resume": next_node,
            };
            update["escalations"] = state.get("escalations", []) + [escalation];
            update["resume_target"] = next_node;
            print(f"[流水线·{unit_name}] 步骤{step}出现异常，已登记升级！");
        else:
            print(f"[流水线·{unit_name}] 步骤{step_index}/{step_count} {step}完成！");
        return update;

    return step_node;


def build_pipeline_subgraph(unit: dict, online_mode: bool, next_node: str):
    # 步骤按配置顺序直连，没有分支，出错也先把步骤走完再交给顶层判断。
    graph = StateGraph(HybridState);
    step_node_names = [];
    steps = unit["steps"];
    for index, step in enumerate(steps, start=1):
        node_name = f"{unit['key']}_step{index}";
        graph.add_node(
            node_name,
            make_pipeline_step_node(unit, step, index, len(steps), online_mode, next_node),
        );
        step_node_names.append(node_name);
    graph.add_edge(START, step_node_names[0]);
    for index in range(len(step_node_names) - 1):
        graph.add_edge(step_node_names[index], step_node_names[index + 1]);
    graph.add_edge(step_node_names[-1], END);
    return graph.compile();


def supervisor_node(state: HybridState) -> dict:
    # 混合架构的拓扑是设计期定的，这里只按配置下发，不做运行时重构。
    print("[总控] 承接整体目标，向下拆解业务单元！");
    units = [
        {"name": unit["name"], "goal": unit["goal"], "steps": unit["steps"]}
        for unit in V_单元配置
    ];
    plan_text = "；".join(f"{item['name']}：{item['goal']}" for item in units);
    print(f"[总控] 下发计划：{plan_text}！");
    return {
        "blackboard": {"task": state["task"], "plan": plan_text},
        "round_opinions": [],
        "unit_records": [],
        "escalations": [],
        "pipeline_error": "",
        "terminated": False,
    };


def node_label(node_id: str) -> str:
    # 节点标识是英文的，日志里翻译成中文，免得看晕。
    labels = {"summary": "全局汇总", "human_fallback": "人工兜底", "supervisor": "总控拆解"};
    for unit in V_单元配置:
        labels[f"{unit['key']}_roundtable"] = f"{unit['name']}圆桌";
        labels[f"{unit['key']}_pipeline"] = f"{unit['name']}流水线";
    return labels.get(node_id, node_id);


def human_node(state: HybridState) -> dict:
    # 评审卡壳或执行异常时接管。非交互环境（测试、CI）默认继续，避免卡在 input 上。
    escalations = state.get("escalations", []);
    last_escalation = escalations[-1] if escalations else {};
    resume_target = state.get("resume_target", "summary");
    print("——" * 50);
    print(f"[人工兜底] 收到升级：{last_escalation.get('reason', '未知原因')}！");
    print(f"[人工兜底] 若继续，将回到节点：{node_label(resume_target)}！");
    decision = "继续";
    if sys.stdin.isatty():
        try:
            answer = input("请输入决定（继续/终止）：").strip();
            if answer in ("终止", "中止", "stop", "quit"):
                decision = "终止";
        except EOFError:
            print("[人工兜底] 没有读到人工输入，默认选择继续！");
    else:
        print("[人工兜底] 当前不是交互终端，默认选择继续！");
    terminated = decision == "终止";
    blackboard = {
        "human_decisions": [{
            "reason": last_escalation.get("reason", ""),
            "decision": decision,
        }],
    };
    print(f"[人工兜底] 人工决定：{decision}！");
    return {
        "human_decision": decision,
        "terminated": terminated,
        "blackboard": blackboard,
    };


def human_route(state: HybridState) -> str:
    # 终止就收尾，继续就回到升级时登记的节点。
    if state.get("terminated"):
        return "summary";
    return state.get("resume_target", "summary");


def summary_node(state: HybridState) -> dict:
    # 报告只放关键结论：各单元交付、风险、升级记录。
    records = state.get("unit_records", []);
    escalations = state.get("escalations", []);
    blackboard = state.get("blackboard", {});
    risks = blackboard.get("risks", []);
    lines = [f"任务：{state.get('task', '')}"];
    lines.append(f"交付单元数量：{len(records)}");
    for record in records:
        latest = record["artifacts"][-1] if record["artifacts"] else "无产物";
        lines.append(f"- {record['unit']}（{record['status']}）：{latest}");
    if risks:
        lines.append("风险清单：");
        for risk in risks:
            lines.append(f"- {risk}");
    if escalations:
        lines.append("升级记录：");
        for escalation in escalations:
            lines.append(f"- {escalation['kind']}：{escalation['reason']}");
    if state.get("terminated"):
        lines.append("流程状态：人工终止，未完成全部单元。");
    else:
        lines.append("流程状态：全部单元按预定流水线交付完成。");
    report = "\n".join(lines);
    print("[汇总] 全局报告已生成！");
    return {"final_report": report, "blackboard": {"final_report": report}};


def roundtable_gate(state: HybridState) -> str:
    # 通过就去流水线，其余情况交给人。
    if state.get("review_verdict") == "通过":
        return "通过";
    return "升级";


def pipeline_gate(state: HybridState) -> str:
    # 没有异常就继续下一单元，有异常交给人。
    if state.get("pipeline_error"):
        return "升级";
    return "继续";


def build_hybrid_graph(online_mode: bool, force_reject_unit: str = ""):
    # 下一站按单元顺序推，最后一个接汇总。人工兜底可以回到任意单元的圆桌或流水线。
    graph = StateGraph(HybridState);
    unit_keys = [unit["key"] for unit in V_单元配置];
    next_nodes = {};
    for index, unit in enumerate(V_单元配置):
        if index + 1 < len(V_单元配置):
            next_nodes[unit["key"]] = f"{V_单元配置[index + 1]['key']}_roundtable";
        else:
            next_nodes[unit["key"]] = "summary";
    graph.add_node("supervisor", supervisor_node);
    for unit in V_单元配置:
        force_reject = unit["name"] == force_reject_unit;
        graph.add_node(
            f"{unit['key']}_roundtable",
            build_roundtable_subgraph(unit, online_mode, force_reject),
        );
        graph.add_node(
            f"{unit['key']}_pipeline",
            build_pipeline_subgraph(unit, online_mode, next_nodes[unit["key"]]),
        );
    graph.add_node("human_fallback", human_node);
    graph.add_node("summary", summary_node);
    graph.add_edge(START, "supervisor");
    graph.add_edge("supervisor", f"{unit_keys[0]}_roundtable");
    for unit in V_单元配置:
        unit_key = unit["key"];
        graph.add_conditional_edges(
            f"{unit_key}_roundtable",
            roundtable_gate,
            {"通过": f"{unit_key}_pipeline", "升级": "human_fallback"},
        );
        graph.add_conditional_edges(
            f"{unit_key}_pipeline",
            pipeline_gate,
            {"继续": next_nodes[unit_key], "升级": "human_fallback"},
        );
    human_map = {f"{unit['key']}_pipeline": f"{unit['key']}_pipeline" for unit in V_单元配置};
    human_map.update({f"{unit['key']}_roundtable": f"{unit['key']}_roundtable" for unit in V_单元配置[1:]});
    human_map["summary"] = "summary";
    graph.add_conditional_edges("human_fallback", human_route, human_map);
    graph.add_edge("summary", END);
    return graph.compile();


def build_initial_state(task: str) -> dict:
    # 初始黑板只放任务和空的风险清单，其余字段在流程里慢慢长出来。
    return {
        "task": task,
        "blackboard": {"task": task, "risks": []},
        "round_index": 1,
        "round_opinions": [],
        "review_verdict": "",
        "review_reason": "",
        "pipeline_artifacts": [],
        "pipeline_error": "",
        "unit_records": [],
        "escalations": [],
        "resume_target": "",
        "human_decision": "",
        "terminated": False,
        "final_report": "",
    };


def parse_args(argv: list = None) -> argparse.Namespace:
    # argv 为空时读系统参数，测试里可以直接传列表。
    parser = argparse.ArgumentParser(description="多智能体混合组合架构演示：层级总控 + 圆桌评审 + 流水线执行 + 全局黑板");
    parser.add_argument("--task", default=V_示例任务, help="要交付的整体任务");
    parser.add_argument("--offline", action="store_true", help="强制使用离线规则大脑");
    parser.add_argument("--online", action="store_true", help="强制使用在线大模型，需要 OPENAI_API_KEY");
    parser.add_argument("--fail-unit", default="", help="指定一个持续评审不通过的业务单元，用于演练升级链路");
    parser.add_argument("--mermaid", action="store_true", help="只导出流程图，不执行任务");
    parser.add_argument("--json", action="store_true", help="结束时打印完整状态 JSON");
    return parser.parse_args(argv);


def main(argv: list = None) -> None:
    # 密钥存在默认就走在线，--offline 可以强制降级。
    args = parse_args(argv);
    if args.online and not V_接口密钥:
        print("[提示] 没有检测到 OPENAI_API_KEY，本次自动使用离线大脑！");
    online_mode = bool(V_接口密钥) and not args.offline;
    force_reject_unit = args.fail_unit.strip();
    if force_reject_unit and force_reject_unit not in [unit["name"] for unit in V_单元配置]:
        print(f"[提示] 未知单元 {force_reject_unit}，可选项：{'、'.join(unit['name'] for unit in V_单元配置)}！");
        return;
    app = build_hybrid_graph(online_mode, force_reject_unit);
    if args.mermaid:
        print(app.get_graph().draw_mermaid());
        return;
    print("——" * 50);
    print(f"混合组合架构启动，模式：{'在线大模型' if online_mode else '离线演示'}！");
    print(f"任务：{args.task}！");
    final_state = app.invoke(build_initial_state(args.task));
    print("——" * 50);
    print(final_state["final_report"]);
    status = "人工终止" if final_state.get("terminated") else "正常完成";
    print(f"✅ 流程结束，状态：{status}，交付单元：{len(final_state.get('unit_records', []))} 个！");
    if args.json:
        print(json.dumps(final_state, ensure_ascii=False, indent=2, default=str));


if __name__ == "__main__":
    main();

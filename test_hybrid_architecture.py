"""
流程测试，全部走离线大脑，不碰网络。

python -m unittest -v test_hybrid_architecture.py
"""

import io;
import unittest;
from contextlib import redirect_stdout;

import hybrid_architecture as hybrid;

# 每个用例都用同一个任务，差异只在参数。
V_测试任务 = "为电商 App 设计一个限时秒杀功能，要求扛住高并发并给出回滚预案。";


def run_offline(force_reject_unit: str = "") -> dict:
    # 跑完整图，顺手把 stdout 收走，免得测试输出刷屏。
    app = hybrid.build_hybrid_graph(False, force_reject_unit);
    initial_state = hybrid.build_initial_state(V_测试任务);
    buffer = io.StringIO();
    with redirect_stdout(buffer):
        final_state = app.invoke(initial_state);
    return final_state;


class HybridArchitectureTest(unittest.TestCase):

    def test_offline_full_run(self):
        # 三个单元都该交付完成，不该出现升级。
        final_state = run_offline();
        records = final_state["unit_records"];
        self.assertEqual(len(records), 3);
        self.assertTrue(all(record["status"] == "交付完成" for record in records));
        self.assertEqual(final_state["escalations"], []);
        self.assertEqual(final_state["pipeline_error"], "");
        self.assertIn(V_测试任务, final_state["final_report"]);

    def test_blackboard_shared_across_subgraphs(self):
        # 评审结论、单元产物、最终报告要能同时留在黑板上。
        final_state = run_offline();
        blackboard = final_state["blackboard"];
        self.assertIn("reviews", blackboard);
        self.assertEqual(set(blackboard["artifacts"].keys()), {"产品单元", "技术单元", "交付单元"});
        self.assertEqual(blackboard["final_report"], final_state["final_report"]);
        self.assertIn("关键链路缺少限流与降级策略。", blackboard["risks"]);

    def test_roundtable_second_round(self):
        # 技术单元第一轮被安全专家驳回，第二轮放行。
        final_state = run_offline();
        reviews = final_state["blackboard"]["reviews"];
        self.assertEqual(reviews["技术单元·第1轮"]["verdict"], "驳回");
        self.assertEqual(reviews["技术单元·第2轮"]["verdict"], "通过");

    def test_escalation_to_human(self):
        # 强制驳回三轮后应该升级，人工选继续后流程照样走完。
        final_state = run_offline("技术单元");
        escalations = final_state["escalations"];
        self.assertEqual(len(escalations), 1);
        self.assertEqual(escalations[0]["kind"], "评审卡壳");
        self.assertEqual(final_state["human_decision"], "继续");
        self.assertEqual(final_state["blackboard"]["human_decisions"][0]["decision"], "继续");
        self.assertEqual(len(final_state["unit_records"]), 3);
        self.assertIn("升级记录：", final_state["final_report"]);

    def test_mermaid_structure(self):
        # 图里要能看到三层节点和人工兜底。
        app = hybrid.build_hybrid_graph(False, "");
        mermaid_text = app.get_graph().draw_mermaid();
        for node_name in ("supervisor", "product_roundtable", "tech_pipeline", "delivery_pipeline", "human_fallback", "summary"):
            self.assertIn(node_name, mermaid_text);

    def test_parse_json_block(self):
        # 模型回复外面常裹着代码围栏，解析要能穿过它。
        content = '以下是结果：\n```json\n{"意见": "方案可行", "结论": "通过", "风险": []}\n```';
        data = hybrid.parse_json_block(content);
        self.assertEqual(data["结论"], "通过");
        self.assertEqual(hybrid.parse_json_block("完全不是 JSON"), {});


if __name__ == "__main__":
    unittest.main();

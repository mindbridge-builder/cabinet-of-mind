from pathlib import Path
import unittest


UI = Path(__file__).resolve().parents[1] / "ui" / "index.html"


class UIThinkingTests(unittest.TestCase):
    def test_thinking_state_is_per_agent_with_stages(self):
        html = UI.read_text(encoding="utf-8")

        self.assertIn("const activeThinking = new Map();", html)
        self.assertIn("case 'thinking_stage': updateThinkingStage", html)
        self.assertIn("function renderThinkingRows", html)
        self.assertIn("function thinkingStageLabel", html)
        self.assertIn("agent === 'gol' ? 'working' : 'thinking'", html)
        self.assertIn("roleAgent(d.message?.role)", html)
        self.assertIn("removeThinking(_msgAgent);", html)
        # After an agent's reply, the UI immediately requests health — limit bars
        # update without waiting for the 10-minute tick.
        self.assertIn("sendAction('health');", html)
        self.assertIn("renderThinkingRows(c, false)", html)


if __name__ == "__main__":
    unittest.main()

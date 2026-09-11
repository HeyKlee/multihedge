import unittest
from unittest.mock import patch, Mock
import os
os.environ['OPENROUTER_API_KEY'] = 'test-key'
from agent_architecture import AnalystAgent, ResearcherAgent, TraderAgent

class TestAnalystAgent(unittest.TestCase):
    @patch('agent_architecture._openrouter_chat')
    def test_run_returns_expected_structure(self, mock_chat):
        # Mock the OpenRouter response
        mock_chat.return_value = {
            'choices': [{
                'message': {
                    'content': '{"bias_read": "bullish", "news_signal": 0.5, "price_deviation_pct": 2.3, "recommendation": "buy"}'
                }
            }]
        }

        agent = AnalystAgent()
        result = agent.run('TEST', {'deviation_pct': 1.0, 'trend': 'up'}, {'bias_label': 'neutral', 'bias_score': 0.0})

        self.assertIn('bias_read', result)
        self.assertIn('news_signal', result)
        self.assertIn('price_deviation_pct', result)
        self.assertIn('recommendation', result)
        self.assertEqual(result['bias_read'], 'bullish')
        self.assertEqual(result['news_signal'], 0.5)
        self.assertEqual(result['price_deviation_pct'], 2.3)
        self.assertEqual(result['recommendation'], 'buy')

class TestResearcherAgent(unittest.TestCase):
    @patch('agent_architecture._openrouter_chat')
    def test_run_returns_expected_structure(self, mock_chat):
        mock_chat.return_value = {
            'choices': [{
                'message': {
                    'content': '{"bull_case_score": 0.7, "bear_case_score": 0.3, "debate_outcome": "bull", "confidence": 0.8}'
                }
            }]
        }

        agent = ResearcherAgent()
        result = agent.run('TEST', {'bias_read': 'bullish', 'news_signal': 0.5, 'price_deviation_pct': 2.3, 'recommendation': 'buy'})

        self.assertIn('bull_case_score', result)
        self.assertIn('bear_case_score', result)
        self.assertIn('debate_outcome', result)
        self.assertIn('confidence', result)
        self.assertEqual(result['bull_case_score'], 0.7)
        self.assertEqual(result['bear_case_score'], 0.3)
        self.assertEqual(result['debate_outcome'], 'bull')
        self.assertEqual(result['confidence'], 0.8)

class TestTraderAgent(unittest.TestCase):
    def test_run_returns_expected_structure(self):
        # TraderAgent does not make API calls, so we can test directly
        agent = TraderAgent()
        researcher_output = {
            'bull_case_score': 0.8,
            'bear_case_score': 0.2,
            'debate_outcome': 'bull',
            'confidence': 0.9
        }
        risk_state = {'POSITION_FRACTION': 0.5}
        result = agent.run('TEST', researcher_output, risk_state)

        self.assertIn('coin', result)
        self.assertIn('action', result)
        self.assertIn('quantity_pct', result)
        self.assertIn('confidence', result)
        self.assertIn('reason', result)
        self.assertEqual(result['coin'], 'TEST')
        self.assertEqual(result['action'], 'buy')
        self.assertEqual(result['quantity_pct'], 40.0)  # 0.8 * 0.5 * 100
        self.assertEqual(result['confidence'], 0.9)
        self.assertEqual(result['reason'], 'validated_debate')

if __name__ == '__main__':
    unittest.main()
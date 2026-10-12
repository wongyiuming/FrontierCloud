"""Exercise the actual Admin renderer against controller diagnostic vocabulary."""
import ast
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


def controller_codes():
    tree = ast.parse((ROOT / 'app/services/certmanager/manager.py').read_text(encoding='utf-8'))
    classifier = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'classify')
    codes = set()
    for node in ast.walk(classifier):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            codes.add(node.value.value)
        if isinstance(node, ast.Dict):
            codes.update(value.value for value in node.values if isinstance(value, ast.Constant) and isinstance(value.value, str))
        if isinstance(node, ast.Tuple):
            codes.update(value.value for value in node.elts if isinstance(value, ast.Constant) and isinstance(value.value, str))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'BusinessIngressPending':
            codes.add(node.args[0].value)
        if isinstance(node, ast.keyword) and node.arg == 'error' and isinstance(node.value, ast.Constant) and node.value.value:
            codes.add(node.value.value)
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'invalid' for target in node.targets):
            if isinstance(node.value, ast.Constant) and node.value.value:
                codes.add(node.value.value)
    codes.add('clock_unreliable')
    admin = ast.parse((ROOT / 'app/api/v1/admin_nodes.py').read_text(encoding='utf-8'))
    for node in ast.walk(admin):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == 'last_error' and isinstance(value, ast.Constant):
                    codes.add(value.value)
    return codes


class CertificateDiagnosticContractTests(unittest.TestCase):
    def render(self, codes):
        result = subprocess.run(['node', str(ROOT / 'tests/node_certificate_diagnostics.mjs')],
            cwd=ROOT, input=json.dumps(codes), text=True, encoding='utf-8', capture_output=True, check=True)
        return {row['code']: row['message'] for row in json.loads(result.stdout)}

    def test_controller_codes_have_actionable_actual_panel_messages(self):
        codes = controller_codes() | {'storage_offline', 'authenticated_observation_unavailable'}
        messages = self.render(sorted(codes))
        self.assertEqual(set(messages), codes)
        for code, message in messages.items():
            with self.subTest(code=code):
                self.assertNotIn('未知错误', message)
                self.assertNotIn('未识别诊断', message)
                self.assertNotEqual(message, '无')
        self.assertIn('DNS', messages['dns_unavailable'])
        self.assertIn('80', messages['port80_unavailable'])
        self.assertIn('CAA', messages['caa_denied'])
        self.assertIn('退避', messages['ca_rate_limited'])
        self.assertIn('握手确认', messages['awaiting_serving_confirmation'])

    def test_unknown_diagnostics_are_bounded_and_never_render_markup(self):
        messages = self.render(['future_diagnostic', 'constructor', '__proto__', '<img src=x onerror=alert(1)>', 'x' * 81, ''])
        self.assertEqual(messages['future_diagnostic'], '未识别诊断：future_diagnostic')
        self.assertEqual(messages['constructor'], '未识别诊断：constructor')
        self.assertEqual(messages['__proto__'], '未识别诊断：__proto__')
        self.assertEqual(messages['<img src=x onerror=alert(1)>'], '未知错误')
        self.assertEqual(messages['x' * 81], '未知错误')
        self.assertEqual(messages[''], '无')


if __name__ == '__main__':
    unittest.main()

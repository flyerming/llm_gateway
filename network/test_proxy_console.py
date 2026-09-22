"""Offline regression tests: python -m unittest discover -s network -p test_proxy_console.py."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest


spec = importlib.util.spec_from_file_location(
    "proxy_console", Path(__file__).with_name("proxy-console.py")
)
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


class AccountSummaryTests(unittest.TestCase):
    def summary(self, used):
        return console._account_summary({
            "quota": {"signals": {"X-Codex-Primary-Used-Percent": used}},
            "access_token": "secret",
        })

    def test_used_and_remaining(self):
        for value, expected in [("73", 27), ("73.5", 26.5), (0, 100), (100, 0)]:
            with self.subTest(value=value):
                result = self.summary(value)
                self.assertEqual(result["remaining_percent"], expected)
                self.assertNotIn("access_token", result)

    def test_invalid_usage_is_unknown(self):
        for value in [None, "", "bad", "NaN", "Infinity", -1, 101, True]:
            with self.subTest(value=value):
                self.assertNotIn("used_percent", self.summary(value))

    def test_error_is_preserved(self):
        result = console._account_summary({"status": "error", "status_message": "overloaded"})
        self.assertEqual(result["status_message"], "overloaded")

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for frontend regression tests")
    def test_browser_logic(self):
        # Check the actual PAGE sent to the browser, not the Python source literal.
        script = console.PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
        harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const script = JSON.parse(fs.readFileSync(0, 'utf8'));
new vm.Script(script); // Syntax check of the complete served script.
const elements = {};
const $ = id => elements[id] ||= {textContent: '', value: '', disabled: false};
let toasts = [];
let calls = 0;
let fail = false;
const context = vm.createContext({
  $, Date, Math, Number, Array, String,
  toast: (message, error) => toasts.push({message, error}),
  setCodexStatus: (text, cls) => { $('codexStatus').textContent = text; },
  api: async () => {
    calls++;
    if (fail) throw new Error('offline');
    return { files: [{ used_percent: 73, remaining_percent: 27, status: 'error',
                      status_message: 'overloaded' }] };
  },
});
vm.runInContext(script.slice(script.indexOf('function codexLabel'),
                            script.indexOf('async function pollCodexLogin')), context);
(async () => {
  await context.refreshCodexAccounts(true);
  assert.match($('codexAccounts').textContent, /已用 73%（剩余 27%）/);
  assert.match($('codexAccounts').textContent, /非本次自检结果/);
  assert.equal(toasts.at(-1).error, undefined);
  assert.equal(context.codexHealth({status: 'error'}).rank, 2);
  assert.match(context.codexDetail({}), /用量未知/);
  fail = true;
  toasts = [];
  await context.refreshCodexAccounts(true);
  assert.equal(toasts.length, 1);
  assert.equal(toasts[0].error, true);
  assert.match($('codexAccountsUpdated').textContent, /刷新失败/);
  assert.equal($('codexRefresh').disabled, false);
  fail = false;
  const before = calls;
  await Promise.all([context.refreshCodexAccounts(false), context.refreshCodexAccounts(false)]);
  assert.equal(calls, before + 1);
  let events = [];
  context.api = async () => { events.push('probe'); return {steps: []}; };
  context.renderSelftest = () => events.push('render');
  context.refreshCodexAccounts = async () => events.push('accounts');
  context.esc = String;
  vm.runInContext(script.slice(script.indexOf('async function runSelftest'),
                              script.indexOf('async function switchTo')), context);
  await context.runSelftest(true);
  assert.deepEqual(events, ['probe', 'render', 'accounts']);
  assert.equal($('selftestProbe').disabled, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            ["node", "-e", harness], input=json.dumps(script),
            text=True, capture_output=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

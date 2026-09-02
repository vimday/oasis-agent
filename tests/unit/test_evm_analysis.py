import unittest

from oasis_agent.evm import decode_hook_permissions, extract_push4_selectors


class EvmAnalysisTests(unittest.TestCase):
    def test_selector_scanner_skips_push_payload(self) -> None:
        bytecode = bytes.fromhex("63deadbeef6563abcdef0102")
        selectors = extract_push4_selectors(bytecode)
        self.assertEqual([item.selector for item in selectors], ["0xdeadbeef"])

    def test_benchmark_permissions(self) -> None:
        permissions = decode_hook_permissions("0x1191Ac2561817686EFE4296B37BA5a5419Eaa880")
        self.assertTrue(permissions.before_initialize)
        self.assertTrue(permissions.before_add_liquidity)
        self.assertTrue(permissions.before_swap)
        self.assertFalse(permissions.after_swap)
        self.assertFalse(permissions.before_swap_returns_delta)


if __name__ == "__main__":
    unittest.main()

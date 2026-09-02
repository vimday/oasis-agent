class OasisError(Exception):
    """Base error for Oasis Agent."""


class ConfigurationError(OasisError):
    pass


class RpcError(OasisError):
    pass


class EvidenceError(OasisError):
    pass


class FeeEncodingError(OasisError):
    pass


class AccountingError(OasisError):
    pass


class ExecutionBlockedError(OasisError):
    pass

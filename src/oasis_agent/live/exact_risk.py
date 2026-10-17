"""Owner trial-1 loss convention in integer USD micros, independent of display floats."""

from dataclasses import dataclass
from fractions import Fraction


@dataclass(slots=True)
class ExactRisk:
    basis: int | None = None
    external: int = 0
    checkpoint: int | None = None
    checkpoint_at: int = 0
    last_at: int = 0
    day: str | None = None
    daily_locked: int = 0
    last_nav: int | None = None
    migrated_legacy: bool = False

    def validate(self) -> None:
        for name in (
            "basis",
            "checkpoint",
            "last_nav",
            "external",
            "daily_locked",
            "checkpoint_at",
            "last_at",
        ):
            value = getattr(self, name)
            if value is None and name in {"basis", "checkpoint", "last_nav"}:
                continue
            if type(value) is not int or abs(value) >= 2**256 or (name != "external" and value < 0):
                raise ValueError("invalid integer risk checkpoint")
        if type(self.migrated_legacy) is not bool:
            raise ValueError("invalid risk migration flag")
        if (self.basis is None) != (self.checkpoint is None):
            raise ValueError("partial integer risk baseline")
        if self.day is not None and (not isinstance(self.day, str) or len(self.day) != 10):
            raise ValueError("invalid risk day")

    def observe(
        self, *, day: str, at: int, nav: int, flow: int = 0, checkpoint: bool = False
    ) -> tuple[int, int]:
        self.validate()
        if any(type(v) is not int for v in (at, nav, flow)) or nav < 0 or at < self.last_at:
            raise ValueError("invalid or regressed risk observation")
        new_day = self.day != day
        if self.basis is None:
            if flow:
                raise ValueError("inception cannot contain an external flow")
            self.basis = self.checkpoint = nav
            self.checkpoint_at = at
        assert self.checkpoint is not None
        if new_day:
            self.day, self.daily_locked = day, 0
        self.external += flow
        self.checkpoint += flow
        if self.checkpoint < 0 or self.basis + self.external < 0:
            raise ValueError("external withdrawal exceeds tracked capital")
        if new_day or checkpoint or at - self.checkpoint_at >= 3600:
            self.daily_locked += max(0, self.checkpoint - nav)
            self.checkpoint, self.checkpoint_at = nav, at
        self.last_at, self.last_nav = at, nav
        self.validate()
        return self.losses(nav)

    def losses(self, nav: int) -> tuple[int, int]:
        self.validate()
        if type(nav) is not int or nav < 0 or self.basis is None or self.checkpoint is None:
            raise ValueError("exact loss calculation needs an observed baseline")
        return (
            self.daily_locked + max(0, self.checkpoint - nav),
            max(0, self.basis + self.external - nav),
        )


def legacy_ceiling(value: float | int) -> int:
    """Conservative migration of a finite historical USD declaration, never reset it."""
    if type(value) not in (float, int):
        raise ValueError("invalid legacy dollar declaration")
    micros = Fraction(str(value)) * 1_000_000
    return -(-micros.numerator // micros.denominator)

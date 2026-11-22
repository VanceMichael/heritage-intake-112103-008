"""Rights evaluation.

A license answers one question: may *this digital carrier* be shown on *this
channel* on *this date*?  Allowed use is matched exactly against the channel
name; a grant for ``research`` never implies ``online-exhibition``.  Validity
is inclusive of both endpoints and evaluated in the recording institution's
UTC-offset-neutral calendar (dates are compared as dates).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .records import License


@dataclass(frozen=True)
class RightsDecision:
    allowed: bool
    reason: str
    license_ref: str | None
    channel: str | None
    evaluated_on: date | None
    restrictions: str | None = None

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "license_ref": self.license_ref,
            "channel": self.channel,
            "evaluated_on": self.evaluated_on.isoformat()
            if self.evaluated_on
            else None,
            "restrictions": self.restrictions,
        }


def evaluate_license(
    license: License | None,
    channel: str | None,
    on: date | None,
) -> RightsDecision:
    if license is None:
        return RightsDecision(
            allowed=False,
            reason="没有可用的许可记录",
            license_ref=None,
            channel=channel,
            evaluated_on=on,
        )
    if license.revoked:
        return RightsDecision(
            allowed=False,
            reason="许可已被撤销",
            license_ref=license.license_ref,
            channel=channel,
            evaluated_on=on,
        )
    if not channel:
        return RightsDecision(
            allowed=False,
            reason="未指定用途渠道，无法判断许可范围",
            license_ref=license.license_ref,
            channel=channel,
            evaluated_on=on,
        )
    if channel not in license.allowed_use:
        return RightsDecision(
            allowed=False,
            reason=f"许可未覆盖渠道 {channel!r}",
            license_ref=license.license_ref,
            channel=channel,
            evaluated_on=on,
        )
    if on is None:
        return RightsDecision(
            allowed=False,
            reason="未指定评估日期，无法判断许可有效期",
            license_ref=license.license_ref,
            channel=channel,
            evaluated_on=on,
        )
    if license.valid_from and on < license.valid_from:
        return RightsDecision(
            allowed=False,
            reason=f"许可自 {license.valid_from.isoformat()} 起生效",
            license_ref=license.license_ref,
            channel=channel,
            evaluated_on=on,
        )
    if license.valid_until and on > license.valid_until:
        return RightsDecision(
            allowed=False,
            reason=f"许可已于 {license.valid_until.isoformat()} 到期",
            license_ref=license.license_ref,
            channel=channel,
            evaluated_on=on,
        )
    return RightsDecision(
        allowed=True,
        reason="许可有效且覆盖该渠道",
        license_ref=license.license_ref,
        channel=channel,
        evaluated_on=on,
        restrictions=license.restrictions,
    )


def select_effective_license(
    licenses: Iterable[License],
    asset_ref: str,
    on: date | None,
) -> License | None:
    """Choose the license that governs an asset for a new evaluation.

    Rules, in order:
    * only licenses for the requested asset;
    * a license that supersedes another replaces it outright;
    * among the remainder, prefer one already valid on ``on``;
    * then the highest revision, then the most recently collected.
    """

    candidates = [lic for lic in licenses if lic.asset_ref == asset_ref]
    if not candidates:
        return None
    superseded = {
        lic.supersedes for lic in candidates if lic.supersedes
    }
    effective = [lic for lic in candidates if lic.license_ref not in superseded]
    if not effective:
        effective = list(candidates)

    def sort_key(lic: License):
        in_window = bool(
            (lic.valid_from is None or on is None or lic.valid_from <= on)
            and (lic.valid_until is None or on is None or on <= lic.valid_until)
        )
        return (in_window, lic.revision, lic.collected_at)

    return max(effective, key=sort_key)

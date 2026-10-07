# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Phidipus Agents (see NOTICE)
"""
social/click_verifier.py — Phidipus v2.4 Roadmap C2
═══════════════════════════════════════════════════════════════════

Extracted click verification from vision_actor.py (1994 lines).

This module:
  1. Re-exports ClickVerifier + SiteAssertions from vision/click_verifier.py
  2. Adds social-platform-specific verification helpers
  3. Provides PostClickValidator for workflow-level verification

Architecture:
  - vision/click_verifier.py — core verification engine (JS, AX, Vision)
  - social/click_verifier.py — THIS file: social layer + workflow integration
  - social/vision_actor.py  — imports from HERE (single import path)

Usage:
    from social.click_verifier import (
        ClickVerifier, SiteAssertions,          # re-exported
        PostClickValidator, SocialVerifyPresets, # new
    )

    # Quick social verification
    validator = PostClickValidator(ipc_client=ipc)
    result = await validator.verify_action(
        "facebook_post_submitted",
        fallback_vision="Post published successfully"
    )
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

_log = logging.getLogger("phidipus.social.click_verifier")


def _vlog(icon: str, msg: str) -> None:
    print(f"\033[0;35m[{icon}]\033[0m  {msg}")


# ══════════════════════════════════════════════════════════════
# Re-export core verifier (single import path for vision_actor)
# ══════════════════════════════════════════════════════════════

from vision.click_verifier import ClickVerifier, SiteAssertions, VerifyResult

__all__ = [
    "ClickVerifier", "SiteAssertions", "VerifyResult",
    "PostClickValidator", "SocialVerifyPresets",
]


# ══════════════════════════════════════════════════════════════
# Social-specific verification presets
# ══════════════════════════════════════════════════════════════

class SocialVerifyPresets:
    """
    Pre-built verification configs for common social media actions.

    Each preset returns a dict compatible with ClickVerifier.verify() params:
    {js_assertions, vision_check, timeout_s, description}
    """

    @staticmethod
    def facebook_post() -> dict:
        return {
            "js_assertions": SiteAssertions.fb_post_submitted(),
            "vision_check": "Post published, composer dialog closed",
            "timeout_s": 5.0,
            "description": "Facebook post submission",
        }

    @staticmethod
    def facebook_upload() -> dict:
        return {
            "js_assertions": SiteAssertions.fb_image_uploaded(),
            "vision_check": "Image thumbnail visible in composer",
            "timeout_s": 8.0,
            "description": "Facebook image upload",
        }

    @staticmethod
    def facebook_composer_open() -> dict:
        return {
            "js_assertions": SiteAssertions.fb_composer_open(),
            "vision_check": "Post composer dialog is open with text input area",
            "timeout_s": 3.0,
            "description": "Facebook composer opened",
        }

    @staticmethod
    def instagram_post() -> dict:
        return {
            "js_assertions": SiteAssertions.ig_post_shared(),
            "vision_check": "Post shared successfully, share dialog closed",
            "timeout_s": 5.0,
            "description": "Instagram post shared",
        }

    @staticmethod
    def instagram_next() -> dict:
        return {
            "js_assertions": SiteAssertions.ig_next_button_visible(),
            "vision_check": "Next step or filter screen visible",
            "timeout_s": 3.0,
            "description": "Instagram next step",
        }

    @staticmethod
    def twitter_post() -> dict:
        return {
            "js_assertions": SiteAssertions.x_tweet_submitted(),
            "vision_check": "Tweet posted, composer closed",
            "timeout_s": 5.0,
            "description": "Twitter/X post submitted",
        }

    @staticmethod
    def twitter_composer_open() -> dict:
        return {
            "js_assertions": SiteAssertions.x_composer_open(),
            "vision_check": "Tweet compose dialog with text area",
            "timeout_s": 3.0,
            "description": "Twitter/X composer opened",
        }

    @staticmethod
    def linkedin_post() -> dict:
        return {
            "js_assertions": [
                "!document.querySelector('[role=dialog]')",
                "document.querySelector('.feed-shared-update-v2')",
            ],
            "vision_check": "Post published in LinkedIn feed",
            "timeout_s": 5.0,
            "description": "LinkedIn post submitted",
        }

    @staticmethod
    def gmail_sent() -> dict:
        return {
            "js_assertions": [
                "document.querySelector('.aO .bAo') || document.body.innerText.includes('sent')",
            ],
            "vision_check": "Email sent confirmation banner visible",
            "timeout_s": 3.0,
            "description": "Gmail email sent",
        }

    @staticmethod
    def shopee_add_cart() -> dict:
        return {
            "js_assertions": [
                "document.querySelector('.cart-drawer, [class*=cart-badge]')",
            ],
            "vision_check": "Item added to cart, cart count increased",
            "timeout_s": 3.0,
            "description": "Shopee add to cart",
        }

    # ── Get preset by name ─────────────────────────────────

    _PRESETS = {
        "facebook_post": facebook_post,
        "facebook_upload": facebook_upload,
        "facebook_composer_open": facebook_composer_open,
        "instagram_post": instagram_post,
        "instagram_next": instagram_next,
        "twitter_post": twitter_post,
        "twitter_composer_open": twitter_composer_open,
        "linkedin_post": linkedin_post,
        "gmail_sent": gmail_sent,
        "shopee_add_cart": shopee_add_cart,
    }

    @classmethod
    def get(cls, preset_name: str) -> Optional[dict]:
        fn = cls._PRESETS.get(preset_name)
        if fn:
            return fn()
        return None

    @classmethod
    def available(cls) -> list[str]:
        return list(cls._PRESETS.keys())


# ══════════════════════════════════════════════════════════════
# PostClickValidator — high-level workflow verification
# ══════════════════════════════════════════════════════════════

@dataclass
class ValidationResult:
    """Result of post-click validation."""
    verified: bool = False
    method: str = ""          # "js" | "vision" | "preset" | "timeout"
    confidence: float = 0.0
    elapsed_ms: float = 0.0
    description: str = ""


class PostClickValidator:
    """
    High-level post-click validation for workflow nodes.

    Combines JS assertions, vision verification, and social presets
    into a single verify_action() call.

    Usage:
        validator = PostClickValidator(ipc_client=ipc)
        result = await validator.verify_action("facebook_post")
        if result.verified:
            print("Post confirmed!")
    """

    def __init__(self, ipc_client: Any = None, vision_actor: Any = None) -> None:
        self._ipc = ipc_client
        self._vision = vision_actor
        self._verifier: Optional[ClickVerifier] = None

    def _ensure_verifier(self) -> ClickVerifier:
        if self._verifier is None:
            self._verifier = ClickVerifier(
                ipc_client=self._ipc,
                get_ax_result_fn=None,
            )
        return self._verifier

    async def verify_action(
        self,
        preset_or_description: str,
        fallback_vision: str = "",
        timeout_s: float = 5.0,
    ) -> ValidationResult:
        """
        Verify that a click action succeeded.

        Args:
            preset_or_description: Either a preset name (e.g. "facebook_post")
                                   or a custom vision description
            fallback_vision: Vision description if preset not found
            timeout_s: Max time to wait for verification

        Returns:
            ValidationResult with verified, method, confidence
        """
        t0 = time.perf_counter()

        # Try preset first
        preset = SocialVerifyPresets.get(preset_or_description)
        if preset:
            return await self._verify_preset(preset, t0)

        # Custom vision-based verification
        vision_desc = preset_or_description or fallback_vision
        if vision_desc and self._vision:
            return await self._verify_vision(vision_desc, timeout_s, t0)

        elapsed = (time.perf_counter() - t0) * 1000
        return ValidationResult(
            verified=False, method="none", elapsed_ms=elapsed,
            description="No verification method available",
        )

    async def _verify_preset(self, preset: dict, t0: float) -> ValidationResult:
        """Verify using a social preset config."""
        verifier = self._ensure_verifier()

        # Try JS assertions
        js_assertions = preset.get("js_assertions", [])
        if js_assertions and self._ipc:
            try:
                vr = await verifier.verify(
                    js_verify=js_assertions,
                    vision_verify="",
                    timeout_s=preset.get("timeout_s", 5.0),
                )
                if vr.passed:
                    elapsed = (time.perf_counter() - t0) * 1000
                    return ValidationResult(
                        verified=True, method="js_preset",
                        confidence=vr.confidence,
                        elapsed_ms=elapsed,
                        description=preset.get("description", ""),
                    )
            except Exception:
                pass

        # Fallback to vision
        vision_check = preset.get("vision_check", "")
        if vision_check and self._vision:
            return await self._verify_vision(
                vision_check, preset.get("timeout_s", 5.0), t0
            )

        elapsed = (time.perf_counter() - t0) * 1000
        return ValidationResult(
            verified=False, method="preset_failed",
            elapsed_ms=elapsed,
            description=preset.get("description", ""),
        )

    async def _verify_vision(self, description: str, timeout_s: float,
                             t0: float) -> ValidationResult:
        """Verify using vision (VLM or DINO)."""
        try:
            r = await asyncio.wait_for(
                self._vision.find_element(description),
                timeout=timeout_s,
            )
            elapsed = (time.perf_counter() - t0) * 1000
            if r and r.found:
                return ValidationResult(
                    verified=True, method="vision",
                    confidence=r.confidence,
                    elapsed_ms=elapsed,
                    description=description,
                )
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass

        elapsed = (time.perf_counter() - t0) * 1000
        return ValidationResult(
            verified=False, method="vision_failed",
            elapsed_ms=elapsed, description=description,
        )

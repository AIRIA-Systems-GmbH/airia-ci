# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
"""A function with one branch, so a coverage figure means something."""


def sign(x: int) -> str:
    return "negative" if x < 0 else "non-negative"

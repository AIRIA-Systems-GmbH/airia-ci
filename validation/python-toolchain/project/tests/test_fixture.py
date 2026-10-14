# SPDX-FileCopyrightText: 2026 AIRIA Systems GmbH
# SPDX-License-Identifier: Apache-2.0
from airia_fixture import sign


def test_both_branches():
    assert sign(-1) == "negative"
    assert sign(0) == "non-negative"

"""任务 9.1：SkillRegistry.register 技能名校验（先红）。

- 非空、匹配 ^[A-Za-z0-9_-]{1,64}$：空名/空格/中文/点号斜杠/超长 -> ValueError，
  且非法 Skill 不进入注册表；
- 合法名（含 -/_/数字、恰好 64 字符）注册成功；
- 同名重复注册第二次抛 ValueError，第一个仍保留；
- build_registry() 装配的内置 Skill 名称全部合法且无重名。
"""
from __future__ import annotations

import re

import pytest

from general_agent.skills import Skill, SkillRegistry, build_registry

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _skill_named(name: str) -> Skill:
    class _NamedSkill(Skill):
        pass

    s = _NamedSkill()
    s.name = name
    return s


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "a b",
        "查询",
        "a.b",
        "a/b",
        "a\\b",
        "a:b",
        "工具",
        "a" * 65,
        "a\n",
        "a\n\n",
        "\na",
    ],
)
def test_register_rejects_illegal_name(bad_name):
    registry = SkillRegistry()
    with pytest.raises(ValueError) as exc_info:
        registry.register(_skill_named(bad_name))
    assert bad_name in str(exc_info.value) or repr(bad_name) in str(exc_info.value)
    assert registry.list_all() == []


@pytest.mark.parametrize(
    "good_name",
    [
        "a-b_1",
        "A",
        "9",
        "_",
        "-",
        "a" * 64,
    ],
)
def test_register_accepts_legal_name(good_name):
    registry = SkillRegistry()
    skill = _skill_named(good_name)
    registry.register(skill)
    assert registry.list_all() == [skill]


def test_register_rejects_duplicate_name():
    registry = SkillRegistry()
    first = _skill_named("dup_name")
    second = _skill_named("dup_name")
    registry.register(first)
    with pytest.raises(ValueError) as exc_info:
        registry.register(second)
    assert "dup_name" in str(exc_info.value)
    assert registry.list_all() == [first]


def test_register_case_sensitive_distinction():
    registry = SkillRegistry()
    registry.register(_skill_named("Tool"))
    registry.register(_skill_named("tool"))
    assert len(registry.list_all()) == 2


def test_build_registry_names_legal_and_unique():
    registry = build_registry()
    names = [s.name for s in registry.list_all()]
    assert all(NAME_PATTERN.fullmatch(n) for n in names)
    assert len(names) == len(set(names))

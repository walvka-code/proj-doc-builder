# -*- coding: utf-8 -*-
"""Focused tests for ``tools/validate_doc.py``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import validate_doc as validator

STANDARD_OVERVIEW = (
    "本目录属于测试子系统中的算法模块。"
    "它接收上游协议层输入并调用核心实现完成确定性处理。"
    "结果输出给下游测试组件，且与相邻适配器目录协作。"
)
STANDARD_ROLE = (
    "本目录位于测试子系统的数据层，作为算法模块接收上游协议栈传入的测试向量。"
    "它依赖核心接口完成处理，并把输出结果传递给下游验证组件，同时与同级适配器"
    "协作保持数据格式一致。"
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _standard_document(*, overview: str = STANDARD_OVERVIEW) -> str:
    return f"""# module — 标准测试模块

**导航**: src > **module**

## 概述

{overview}

## 文件清单

| 文件 | 用途 | 关键内容 |
|------|------|---------|
| `__init__.py` | 包入口 | 导出公共接口 |
| `core.py` | 核心实现 | 定义执行流程 |

## 关键符号

| 符号 | 类型 | 定义位置 | 说明 |
|------|------|---------|------|
| `PublicApi` | class | `core.py:L10` | 公共接口 |
| `run_case` | function | `core.py:L20` | 执行用例 |
| `DEFAULT_MODE` | constant | `core.py:L5` | 默认模式 |

## 子目录

| 子目录 | 用途 | 文档 |
|--------|------|------|
| `child/` | 子模块实现 | [child.md](module/child.md) |

## 依赖关系

| 方向 | 模块 | 关系 | 文档 |
|------|------|------|------|
| ⬆ 上游 | parent | 依赖父模块提供的核心接口定义 | [parent.md](parent.md) |

## 系统角色

{STANDARD_ROLE}

## 状态

✅ 完整 — 标准模块已经实现。
"""


def _make_standard_tree(
    tmp_path: Path,
    *,
    overview: str = STANDARD_OVERVIEW,
) -> tuple[Path, Path, Path, Path]:
    project_root = tmp_path / "project"
    docs_root = project_root / "project_docs"
    source_dir = project_root / "src" / "module"
    source_dir.mkdir(parents=True)
    (source_dir / "child").mkdir()
    _write(source_dir / "__init__.py", "# -*- coding: utf-8 -*-\n")
    _write(
        source_dir / "core.py",
        "# -*- coding: utf-8 -*-\n"
        "DEFAULT_MODE = 'test'\n\n"
        "class PublicApi:\n"
        "    pass\n\n"
        "def run_case():\n"
        "    pass\n",
    )
    _write(docs_root / "src" / "parent.md", "# parent\n")
    _write(docs_root / "src" / "module" / "child.md", "# child\n")
    doc_path = docs_root / "src" / "module.md"
    _write(doc_path, _standard_document(overview=overview))
    return project_root, docs_root, source_dir, doc_path


def _validate_standard(
    project_root: Path,
    docs_root: Path,
    source_dir: Path,
    doc_path: Path,
    **kwargs: object,
) -> dict:
    return validator.validate_doc(
        doc_path,
        source_dir,
        project_docs_dir=docs_root,
        project_root=project_root,
        **kwargs,
    )


def _check(result: dict, check_id: int) -> dict:
    return next(item for item in result["checks"] if item["id"] == check_id)


def test_standard_profile_passes_exact_inventory_links_and_content(
    tmp_path: Path,
) -> None:
    project_root, docs_root, source_dir, doc_path = _make_standard_tree(tmp_path)

    result = _validate_standard(project_root, docs_root, source_dir, doc_path)

    assert result["profile"] == validator.PROFILE_STANDARD
    assert result["qualified"] is True
    assert result["ok"] is True
    assert result["failed"] == 0
    assert result["critical_failed"] == 0
    assert len(result["checks"]) == 9
    assert _check(result, 1)["result"] == "PASS"
    assert _check(result, 4)["result"] == "PASS"
    assert _check(result, 9)["result"] == "PASS"


@pytest.mark.parametrize(
    "overview, sentence_count",
    [
        ("本目录属于测试模块。它向下游输出结果。", 2),
        (
            "本目录属于测试模块。它接收输入。它执行处理。它输出结果。它记录状态。",
            5,
        ),
    ],
)
def test_standard_overview_requires_three_to_four_sentences(
    tmp_path: Path,
    overview: str,
    sentence_count: int,
) -> None:
    project_root, docs_root, source_dir, doc_path = _make_standard_tree(
        tmp_path,
        overview=overview,
    )

    result = _validate_standard(project_root, docs_root, source_dir, doc_path)

    overview_check = _check(result, 2)
    assert overview_check["result"] == "FAIL"
    assert f"{sentence_count} 句话" in overview_check["detail"]
    assert overview_check["severity"] == "advisory"
    assert result["critical_failed"] == 0


def test_standard_role_and_dependency_must_be_substantive(tmp_path: Path) -> None:
    project_root, docs_root, source_dir, doc_path = _make_standard_tree(tmp_path)
    content = doc_path.read_text(encoding="utf-8")
    content = content.replace(
        STANDARD_ROLE,
        "这里记录了一段长度足够但没有架构位置和协作含义的普通说明文字，"
        "只是反复描述当前目录已经存在并包含若干内容，不说明实际数据关系。",
    ).replace(
        "依赖父模块提供的核心接口定义",
        "待补充",
    )
    _write(doc_path, content)

    result = _validate_standard(project_root, docs_root, source_dir, doc_path)

    role_check = _check(result, 3)
    dependency_check = _check(result, 9)
    assert role_check["result"] == "FAIL"
    assert "所属子系统、协议层或模块" in role_check["detail"]
    assert dependency_check["result"] == "FAIL"
    assert "关系说明不够具体" in dependency_check["detail"]


def test_placeholder_profile_does_not_require_three_symbols_or_long_role(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    docs_root = project_root / "project_docs"
    source_dir = project_root / "src" / "empty"
    source_dir.mkdir(parents=True)
    _write(source_dir / "__init__.py", "# -*- coding: utf-8 -*-\n")
    _write(docs_root / "src.md", "# src\n")
    doc_path = docs_root / "src" / "empty.md"
    _write(
        doc_path,
        """# empty — 占位包

**导航**: src > **empty**

## 概述

本目录当前为占位。待后续实现。

## 文件清单

| 文件 | 用途 |
|------|------|
| `__init__.py` | 包初始化文件 |

## 系统角色

> 参见 [父目录文档](../src.md)。

## 状态

⬜ 占位 — 目录已建立，待实现。
""",
    )

    result = validator.validate_doc(
        doc_path,
        source_dir,
        project_docs_dir=docs_root,
        project_root=project_root,
    )

    assert result["profile"] == validator.PROFILE_PLACEHOLDER
    assert result["qualified"] is True
    assert _check(result, 2)["result"] == "PASS"
    assert _check(result, 3)["result"] == "PASS"
    assert _check(result, 6)["result"] == "N/A"
    assert _check(result, 9)["result"] == "N/A"


def test_file_manifest_reports_missing_extra_and_duplicate_rows(tmp_path: Path) -> None:
    project_root, docs_root, source_dir, doc_path = _make_standard_tree(tmp_path)
    content = doc_path.read_text(encoding="utf-8")
    content = content.replace(
        "| `core.py` | 核心实现 | 定义执行流程 |",
        "| `ghost.py` | 错误条目 | 不存在 |\n" "| `ghost.py` | 重复错误条目 | 不存在 |",
    )
    _write(doc_path, content)

    result = _validate_standard(project_root, docs_root, source_dir, doc_path)

    file_check = _check(result, 1)
    assert file_check["result"] == "FAIL"
    assert "遗漏文件: core.py" in file_check["detail"]
    assert "多余文件: ghost.py" in file_check["detail"]
    assert "重复文件: ghost.py" in file_check["detail"]
    assert result["critical_failed"] >= 1


def test_subdirectory_manifest_requires_exact_existing_markdown_target(
    tmp_path: Path,
) -> None:
    project_root, docs_root, source_dir, doc_path = _make_standard_tree(tmp_path)
    content = doc_path.read_text(encoding="utf-8").replace(
        "[child.md](module/child.md)",
        "[wrong.md](parent.md)",
    )
    _write(doc_path, content)

    result = _validate_standard(project_root, docs_root, source_dir, doc_path)

    subdir_check = _check(result, 4)
    assert subdir_check["result"] == "FAIL"
    assert "链接目标错误" in subdir_check["detail"]
    assert "src/module/child.md" in subdir_check["detail"]


def test_unsafe_markdown_link_is_a_critical_error(tmp_path: Path) -> None:
    project_root, docs_root, source_dir, doc_path = _make_standard_tree(tmp_path)
    content = doc_path.read_text(encoding="utf-8").replace(
        STANDARD_ROLE,
        STANDARD_ROLE + " [越界链接](../../../outside.md)",
    )
    _write(doc_path, content)

    result = _validate_standard(project_root, docs_root, source_dir, doc_path)

    assert result["qualified"] is False
    assert result["critical"] is True
    assert result["critical_failed"] >= 1
    assert result["error_code"] == "unsafe-markdown-link"
    assert "target-outside-docs-root" in result["error"]


def test_document_and_source_paths_cannot_escape_trusted_roots(tmp_path: Path) -> None:
    project_root, docs_root, source_dir, doc_path = _make_standard_tree(tmp_path)
    outside_doc = tmp_path / "outside.md"
    _write(outside_doc, _standard_document())
    outside_source = tmp_path / "outside-source"
    outside_source.mkdir()

    escaped_doc = validator.validate_doc(
        outside_doc,
        source_dir,
        project_docs_dir=docs_root,
        project_root=project_root,
    )
    escaped_source = validator.validate_doc(
        doc_path,
        outside_source,
        project_docs_dir=docs_root,
        project_root=project_root,
    )
    unbounded_doc = validator.validate_doc(outside_doc, source_dir)

    assert escaped_doc["error_code"] == "path-or-input-error"
    assert "越界" in escaped_doc["error"]
    assert escaped_source["error_code"] == "path-or-input-error"
    assert "越过项目根目录" in escaped_source["error"]
    assert unbounded_doc["error_code"] == "path-or-input-error"
    assert "project_docs" in unbounded_doc["error"]
    assert escaped_doc["critical_failed"] == 1
    assert escaped_source["critical_failed"] == 1
    assert unbounded_doc["critical_failed"] == 1


def test_validate_index_doc_against_root(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    docs_root = project_root / "project_docs"
    docs_root.mkdir(parents=True)
    (project_root / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (project_root / "src").mkdir()
    (docs_root / "src.md").write_text("# src\n", encoding="utf-8")

    index_content = """# index — 项目全息总览

**导航**: **项目全息总览**

## 概述

系统提供统一端到端量化分析与自动交易能力。
微内核分层架构解耦数据采集、因子计算与执行撮合。
通过统一事件总线调度全链路生命周期并交付研报。

## 文件清单

| 文件 | 用途 | 关键内容 |
|---|---|---|
| `main.py` | 统一主入口 | `main` |

## 子目录

| 子目录 | 用途 | 文档 |
|---|---|---|
| `src/` | 核心源码 | [src.md](src.md) |

## 关键符号

| 符号 | 类型 | 定义位置 | 说明 |
|---|---|---|---|
| `main` | function | `main.py:L1` | 启动入口 |

## 依赖关系

| 方向 | 模块 | 关系 | 文档 |
|---|---|---|---|
| ⬇ 下游 | `src` | 驱动核心计算流水线 | [src.md](src.md) |

## 系统角色

本模块作为系统的顶层总控与架构拓扑中枢，统一调用底层微内核、数据中台、动态研判与交易执行交付子系统，为下游提供统一服务接口。

## 状态

✅ 完整
"""
    index_doc = docs_root / "index.md"
    _write(index_doc, index_content)

    result = validator.validate_doc(
        index_doc,
        project_root,
        project_docs_dir=docs_root,
        project_root=project_root,
    )
    assert result["qualified"] is True
    assert result["failed"] == 0
    assert result["critical_failed"] == 0



def test_symbol_table_rejects_hallucinated_symbol(tmp_path: Path) -> None:
    project_root, docs_root, source_dir, doc_path = _make_standard_tree(tmp_path)
    content = doc_path.read_text(encoding="utf-8").replace(
        "| `PublicApi` | class | `core.py:L10` | 公共接口 |",
        "| `HallucinatedClass` | class | `core.py:L10` | 虚构的类 |",
    )
    _write(doc_path, content)

    result = _validate_standard(project_root, docs_root, source_dir, doc_path)
    symbol_check = _check(result, 6)
    assert symbol_check["result"] == "FAIL"
    assert "未在 core.py 的 AST 定义中找到" in symbol_check["detail"]


def test_banned_boilerplate_phrases(tmp_path: Path) -> None:
    project_root, docs_root, source_dir, doc_path = _make_standard_tree(tmp_path)
    content = doc_path.read_text(encoding="utf-8").replace(
        "定义执行流程",
        "核心业务逻辑与基础设施功能",
    )
    _write(doc_path, content)

    result = _validate_standard(project_root, docs_root, source_dir, doc_path)
    banned_check = _check(result, 7)
    assert banned_check["result"] == "FAIL"
    assert "核心业务逻辑与基础设施功能" in banned_check["detail"]



def test_missing_and_invalid_utf8_documents_return_complete_stable_results(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    docs_root = project_root / "project_docs"
    docs_root.mkdir(parents=True)
    source_dir = project_root / "src" / "module"
    source_dir.mkdir(parents=True)
    missing = docs_root / "src" / "missing.md"
    bad_utf8 = docs_root / "src" / "bad.md"
    bad_utf8.parent.mkdir(parents=True)
    bad_utf8.write_bytes(b"# title\n\xff")

    missing_result = validator.validate_doc(
        missing,
        source_dir,
        project_docs_dir=docs_root,
        project_root=project_root,
    )
    bad_result = validator.validate_doc(
        bad_utf8,
        source_dir,
        project_docs_dir=docs_root,
        project_root=project_root,
    )

    expected_keys = {
        "schema_version",
        "file",
        "source_dir",
        "requested_profile",
        "profile",
        "checks",
        "passed",
        "failed",
        "na",
        "critical_failed",
        "critical",
        "qualified",
        "ok",
        "error_code",
        "error",
        "errors",
    }
    assert set(missing_result) == expected_keys
    assert set(bad_result) == expected_keys
    assert len(missing_result["checks"]) == 9
    assert len(bad_result["checks"]) == 9
    assert missing_result["na"] == 9
    assert bad_result["na"] == 9
    assert missing_result["error_code"] == "document-not-found"
    assert bad_result["error_code"] == "invalid-utf8"
    assert missing_result["qualified"] is False
    assert bad_result["qualified"] is False


def test_cli_json_is_pure_and_missing_file_exits_critical(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = tmp_path / "project"
    docs_root = project_root / "project_docs"
    docs_root.mkdir(parents=True)
    source_dir = project_root / "src" / "module"
    source_dir.mkdir(parents=True)
    missing = docs_root / "src" / "missing.md"

    exit_code = validator.main(
        [
            str(missing),
            "--source-dir",
            str(source_dir),
            "--project-docs",
            str(docs_root),
            "--project-root",
            str(project_root),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == validator.EXIT_CRITICAL_ERROR
    assert payload["error_code"] == "document-not-found"
    assert payload["qualified"] is False
    assert captured.err == ""
    assert captured.out.lstrip().startswith("{")
    assert captured.out.rstrip().endswith("}")


def test_cli_exit_policy_distinguishes_advisory_strict_and_critical(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root, docs_root, source_dir, doc_path = _make_standard_tree(
        tmp_path,
        overview="本目录属于测试模块。它向下游输出结果。",
    )
    args = [
        str(doc_path),
        "--source_dir",
        str(source_dir),
        "--project-docs",
        str(docs_root),
        "--project-root",
        str(project_root),
        "--json",
    ]

    advisory_exit = validator.main(args)
    advisory_output = capsys.readouterr()
    strict_exit = validator.main([*args, "--strict"])
    strict_output = capsys.readouterr()

    assert advisory_exit == validator.EXIT_OK
    assert strict_exit == validator.EXIT_VALIDATION_FAILED
    assert json.loads(advisory_output.out)["failed"] == 1
    assert json.loads(strict_output.out)["failed"] == 1
    assert advisory_output.err == ""
    assert strict_output.err == ""

    content = doc_path.read_text(encoding="utf-8").replace(
        "| `core.py` | 核心实现 | 定义执行流程 |",
        "| `ghost.py` | 错误条目 | 不存在 |",
    )
    _write(doc_path, content)
    critical_exit = validator.main(args)
    critical_output = capsys.readouterr()

    assert critical_exit == validator.EXIT_VALIDATION_FAILED
    assert json.loads(critical_output.out)["critical_failed"] >= 1
    assert critical_output.err == ""

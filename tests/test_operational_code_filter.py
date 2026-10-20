"""Tests for the language-agnostic "operational code" classifier.

Operational code = production/runtime source in a recognized programming
language, as opposed to config/data, markup/docs, build/project files,
helper scripts, generated output, and tests. The classifier is deliberately
language-agnostic (extension registry + name/dir heuristics), so it is not
"all .cs" but "all operational code" across C#, Python, Java, TS, Go, etc.
"""
from __future__ import annotations

import pytest

from sentinel.assess.file_filter import (
    is_operational_code,
    is_test_path,
)


@pytest.mark.parametrize("path", [
    "Contoso.Line.Tool/Controller/AcmeEquipmentManager.cs",
    "Contoso.Line.Tool/Controller/AcmeCellManager.cs",
    "src/app/service.py",
    "cmd/main.go",
    "lib/handler.ts",
    "core/Widget.java",
    "engine/render.rs",
    "native/audio.cpp",
])
def test_operational_source_is_operational(path):
    assert is_operational_code(path) is True


@pytest.mark.parametrize("path", [
    "Config/DcConfig.xml",
    "Config/Equipment/InvertedFromToolAcmeModel.xml",
    "appsettings.json",
    "config/app.yaml",
    "settings.ini",
    "data/records.csv",
])
def test_config_and_data_are_not_operational(path):
    assert is_operational_code(path) is False


@pytest.mark.parametrize("path", [
    "Contoso.Line.Tool/Contoso.Line.Tool.csproj",
    "Contoso.Line.Tool.sln",
    "installer/Product.wxs",
    "installer/Setup.wixproj",
    "build.gradle",
])
def test_project_and_build_files_are_not_operational(path):
    assert is_operational_code(path) is False


@pytest.mark.parametrize("path", [
    "scripts/default_ct.vbs",
    "scripts/deploy.ps1",
    "tools/run.sh",
    "tools/build.bat",
])
def test_helper_scripts_are_not_operational(path):
    assert is_operational_code(path) is False


@pytest.mark.parametrize("path", [
    "README.md",
    "docs/guide.rst",
    "web/index.html",
    "web/site.css",
])
def test_docs_and_markup_are_not_operational(path):
    assert is_operational_code(path) is False


@pytest.mark.parametrize("path", [
    "tests/test_service.py",
    "src/service_test.go",
    "Controller/AcmeEquipmentManagerWaferMetadataTddTests.cs",
    "app/widget.spec.ts",
    "app/widget.test.js",
    "core/WidgetTests.cs",
    "spec/models/user_spec.rb",
])
def test_tests_are_not_operational(path):
    assert is_operational_code(path) is False
    assert is_test_path(path) is True


@pytest.mark.parametrize("path", [
    "bin/Debug/App.cs",
    "obj/generated/Model.cs",
    "node_modules/pkg/index.js",
    "packages/vendor/lib.cs",
])
def test_build_and_vendor_are_not_operational(path):
    assert is_operational_code(path) is False


def test_extension_match_is_case_insensitive():
    assert is_operational_code("Controller/Manager.CS") is True


def test_non_test_source_is_not_flagged_as_test():
    assert is_test_path("Controller/AcmeEquipmentManager.cs") is False

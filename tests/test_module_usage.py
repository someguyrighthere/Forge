import pytest

from forge import python_intelligence, tools, ui


def write(tmp_path, source):
    path = tmp_path / "consumer.py"
    path.write_text(source, encoding="utf-8")
    return str(path)


def test_module_usage_tool_is_registered_read_only():
    assert "python_module_usage" in {t["function"]["name"] for t in tools.TOOLS}
    assert "python_module_usage" in tools.READ_ONLY and "python_module_usage" in tools.IMPLS
    assert ui.describe_call("python_module_usage", {"path": "a.py", "module": "b"}) == "a.py b"


def test_module_usage_with_import_module_and_alias(tmp_path):
    path = write(tmp_path, "import project_workflow\nimport project_workflow as pw\n"
                           "x = project_workflow.PENDING_PATH\ny = pw.PENDING_PATH\nz = pw.load()\n")
    result = python_intelligence.python_module_usage(path, "project_workflow")
    assert "uses 2 name(s) from project_workflow" in result
    assert "PENDING_PATH: line 3, line 4" in result and "load: line 5" in result


def test_module_usage_with_from_import_reports_used_and_unused_names(tmp_path):
    path = write(tmp_path, "from helpers import load, save as store, unused\n\ndef run():\n    load()\n    store()\n    load()\n")
    result = python_intelligence.python_module_usage(path, "helpers")
    assert "load: line 1, line 4, line 6" not in result  # the import line itself is not a use
    assert "load: line 4, line 6" in result and "save: line 5" in result and "unused: imported but never used" in result


def test_module_usage_with_package_style_imports_and_module_file_names(tmp_path):
    path = write(tmp_path, "from pkg import tools\nfrom pkg.tools import helper\nvalue = tools.CONST\nhelper()\n")
    result = python_intelligence.python_module_usage(path, "tools.py")
    assert "CONST: line 3" in result and "helper: line 4" in result


def test_module_usage_when_the_module_is_not_imported_or_the_file_is_invalid(tmp_path):
    path = write(tmp_path, "import os\nos.getcwd()\n")
    assert "does not import 'project_workflow'" in python_intelligence.python_module_usage(path, "project_workflow")
    bad = write(tmp_path, "def broken(:\n")
    assert python_intelligence.python_module_usage(bad, "os").startswith("Error: consumer.py has a syntax error")
    with pytest.raises(ValueError, match="Python source file does not exist"):
        python_intelligence.python_module_usage(str(tmp_path / "missing.py"), "os")

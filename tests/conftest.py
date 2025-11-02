import sys
import types

from dotenv import load_dotenv

load_dotenv(override=False)


if "openpyxl" not in sys.modules:
    stub = types.ModuleType("openpyxl")

    def _not_available(*args, **kwargs):  # pragma: no cover - stub for tests
        raise RuntimeError("openpyxl is not available in test environment")

    stub.load_workbook = _not_available
    sys.modules["openpyxl"] = stub


if "docx" not in sys.modules:
    docx_stub = types.ModuleType("docx")

    class _DocxDocument:  # pragma: no cover - stub for tests
        def __init__(self, *args, **kwargs):
            raise RuntimeError("python-docx is not available in test environment")

    docx_oxml = types.ModuleType("docx.oxml")
    docx_oxml.OxmlElement = _not_available
    docx_oxml_ns = types.ModuleType("docx.oxml.ns")
    docx_oxml_ns.qn = _not_available
    docx_oxml.ns = docx_oxml_ns
    docx_shared = types.ModuleType("docx.shared")
    docx_shared.Pt = _not_available
    docx_enum_text = types.ModuleType("docx.enum.text")
    docx_enum_text.WD_ALIGN_PARAGRAPH = types.SimpleNamespace(JUSTIFY=None)
    docx_enum = types.ModuleType("docx.enum")
    docx_enum.text = docx_enum_text

    docx_stub.Document = _DocxDocument
    docx_stub.oxml = docx_oxml
    docx_stub.shared = docx_shared
    docx_stub.enum = docx_enum

    sys.modules["docx"] = docx_stub
    sys.modules["docx.oxml"] = docx_oxml
    sys.modules["docx.oxml.ns"] = docx_oxml_ns
    sys.modules["docx.shared"] = docx_shared
    sys.modules["docx.enum"] = docx_enum
sys.modules["docx.enum.text"] = docx_enum_text



def pytest_ignore_collect(path, config):  # pragma: no cover - collection guard
    return "rfp/__init__.py" in str(path)

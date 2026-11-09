"""Response formatting: the client chooses JSON or XML (lab Stage 1).

Choice order:
  1. ?format=xml or ?format=json in the URL
  2. the Accept header (application/xml or text/xml -> XML)
  3. JSON by default
"""
import xml.etree.ElementTree as ET

from fastapi import Request, Response
from fastapi.responses import JSONResponse


def wants_xml(request: Request) -> bool:
    fmt = request.query_params.get("format")
    if fmt:
        return fmt.lower() == "xml"
    accept = request.headers.get("accept", "").lower()
    if "application/json" in accept or "text/html" in accept:
        return False
    return "application/xml" in accept or "text/xml" in accept


def _build(parent: ET.Element, value) -> None:
    if isinstance(value, dict):
        for key, val in value.items():
            _build(ET.SubElement(parent, str(key)), val)
    elif isinstance(value, (list, tuple)):
        for val in value:
            _build(ET.SubElement(parent, "item"), val)
    elif value is None:
        parent.set("null", "true")
    elif isinstance(value, bool):
        parent.text = "true" if value else "false"
    else:
        parent.text = str(value)


def to_xml(data, root: str) -> str:
    element = ET.Element(root)
    _build(element, data)
    return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(element, encoding="unicode")


def respond(request: Request, data, *, root: str = "response", status: int = 200,
            headers: dict | None = None) -> Response:
    if wants_xml(request):
        return Response(to_xml(data, root), status_code=status,
                        media_type="application/xml", headers=headers)
    return JSONResponse(data, status_code=status, headers=headers)

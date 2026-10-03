"""Read and edit Jupyter notebooks (.ipynb) without a kernel."""
import json
import os
from pathlib import Path

MAX_OUTPUT = 8000
CELL_LIMIT = 1500
OUTPUT_LIMIT = 600
CELL_TYPES = {"code", "markdown", "raw"}
ACTIONS = {"replace", "insert", "delete"}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated {len(text) - limit} characters]"


def _lines(source) -> str:
    return "".join(source) if isinstance(source, list) else str(source or "")


def _load(text: str) -> dict:
    try:
        notebook = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Not a valid notebook (invalid JSON): {error}") from None
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise ValueError("Not a valid notebook: no 'cells' list.")
    return notebook


def _summarize_outputs(outputs: list) -> str:
    parts = []
    for output in outputs:
        kind = output.get("output_type")
        if kind == "stream":
            parts.append(_lines(output.get("text")))
        elif kind in ("execute_result", "display_data"):
            data = output.get("data", {})
            if "text/plain" in data:
                parts.append(_lines(data["text/plain"]))
            else:
                parts.append("<" + ", ".join(data) + ">")
        elif kind == "error":
            parts.append(f"{output.get('ename')}: {output.get('evalue')}")
    return _clip("\n".join(p.rstrip() for p in parts), OUTPUT_LIMIT)


def _path(path: str) -> Path:
    resolved = Path(os.path.expanduser(path)).resolve()
    if resolved.suffix.lower() != ".ipynb" or not resolved.is_file():
        raise ValueError(f"{resolved} is not an existing .ipynb notebook.")
    return resolved


def notebook_read(path: str, start: int = 0, end: int = 0) -> str:
    """Show a notebook's cells (0-based index, type, source) and code-cell outputs."""
    try:
        notebook = _load(_path(path).read_text(encoding="utf-8"))
    except ValueError as error:
        return f"Error: {error}"
    cells = notebook["cells"]
    first = max(int(start or 0), 0)
    last = int(end) if end else len(cells)
    kernel = notebook.get("metadata", {}).get("kernelspec", {}).get("name", "unknown")
    lines = [f"Notebook with {len(cells)} cell(s), kernel: {kernel}. Showing {first}-{min(last, len(cells)) - 1}."]
    for index in range(first, min(last, len(cells))):
        cell = cells[index]
        kind = cell.get("cell_type", "code")
        lines.append(f"\n[{index}] {kind}" + (f" (run #{cell['execution_count']})" if cell.get("execution_count") else ""))
        lines.append(_clip(_lines(cell.get("source")), CELL_LIMIT) or "(empty)")
        if kind == "code" and cell.get("outputs"):
            summary = _summarize_outputs(cell["outputs"])
            if summary:
                lines.append("-- output --\n" + summary)
    return _clip("\n".join(lines), MAX_OUTPUT)


def _new_cell(cell_type: str, source: str) -> dict:
    cell: dict = {"cell_type": cell_type, "metadata": {}, "source": source.splitlines(keepends=True)}
    if cell_type == "code":
        cell.update(execution_count=None, outputs=[])
    return cell


def apply_edit(text: str, action: str, index: int, source: str = "", cell_type: str | None = None) -> tuple[str, str]:
    """Return the notebook JSON after one cell edit, and a short description. Raises ValueError on bad input.

    cell_type defaults to "code" for insert and to the existing cell's type for replace.
    """
    if action not in ACTIONS:
        raise ValueError(f"action must be one of: {', '.join(sorted(ACTIONS))}.")
    if cell_type is not None and cell_type not in CELL_TYPES:
        raise ValueError(f"cell_type must be one of: {', '.join(sorted(CELL_TYPES))}.")
    notebook = _load(text)
    cells, index = notebook["cells"], int(index)
    if action == "insert":
        if not 0 <= index <= len(cells):
            raise ValueError(f"index must be between 0 and {len(cells)} to insert.")
        cells.insert(index, _new_cell(cell_type or "code", source))
        message = f"Inserted a {cell_type or 'code'} cell at index {index}."
    else:
        if not 0 <= index < len(cells):
            raise ValueError(f"index must be between 0 and {len(cells) - 1}; the notebook has {len(cells)} cell(s).")
        if action == "delete":
            del cells[index]
            message = f"Deleted cell {index}."
        else:
            cell = cells[index]
            kind = cell_type or cell.get("cell_type", "code")
            replacement = _new_cell(kind, source)
            # Keep the cell's id and metadata; a code cell's old outputs no longer match its new source.
            for key in ("id", "metadata"):
                if key in cell:
                    replacement[key] = cell[key]
            cells[index] = replacement
            message = f"Replaced cell {index} ({kind}); outputs of a replaced code cell are cleared."
    return json.dumps(notebook, indent=1, ensure_ascii=False) + "\n", message


def outline(text: str) -> str:
    """One line per cell (index, type, first source line): shows the new indices after an insert or delete."""
    cells = _load(text)["cells"]
    lines = []
    for index, cell in enumerate(cells[:40]):
        first = next((l.strip() for l in _lines(cell.get("source")).splitlines() if l.strip()), "(empty)")
        lines.append(f"  [{index}] {cell.get('cell_type', 'code')}: {first[:60]}")
    if len(cells) > 40:
        lines.append(f"  ... {len(cells) - 40} more cell(s)")
    return "Cells now:\n" + "\n".join(lines)


def describe_edit(args: dict) -> str:
    """Text shown when the user approves a notebook edit."""
    action, index = args.get("action", "?"), args.get("index", 0)
    head = f"{action} cell {index}" + (f" ({args['cell_type']})" if args.get("cell_type") else "")
    if action == "delete":
        return head
    return head + "\n" + "\n".join("+ " + line for line in str(args.get("source", "")).splitlines())

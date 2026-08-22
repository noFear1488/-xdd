"""Выгрузка результатов: CSV, JSON и XLSX без внешних зависимостей."""

from __future__ import annotations

import csv
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable, Sequence
from xml.sax.saxutils import escape

from .models import ROW_FIELDS, Business
from .sitecheck import STATUS_TITLES

COLUMN_TITLES: dict[str, str] = {
    "name": "Название",
    "category": "Рубрика",
    "phone": "Телефон",
    "all_phones": "Все телефоны",
    "address": "Адрес",
    "site_status": "Статус сайта",
    "url": "Сайт",
    "site_domain": "Домен",
    "links": "Ссылки",
    "lead_score": "Оценка лида",
    "score_reasons": "Почему лид",
    "rating": "Рейтинг",
    "reviews": "Отзывов",
    "hours": "Режим работы",
    "closed": "Закрыт",
    "lon": "Долгота",
    "lat": "Широта",
    "maps_url": "Найти на Яндекс Картах",
    "source_url": "Объект в источнике",
    "company_id": "ID организации",
    "query": "Поисковый запрос",
}


# Набор колонок для просмотра с телефона: на маленьком экране два десятка
# столбцов бесполезны, нужен минимум для звонка.
MOBILE_FIELDS: list[str] = [
    "name",
    "phone",
    "category",
    "site_status",
    "lead_score",
    "address",
    "maps_url",
]


def resolve_fields(compact: bool = False, fields: list[str] | None = None) -> list[str]:
    if fields:
        unknown = [f for f in fields if f not in ROW_FIELDS]
        if unknown:
            raise ValueError(f"неизвестные колонки: {', '.join(unknown)}")
        return fields
    return MOBILE_FIELDS if compact else ROW_FIELDS


def _rows(businesses: Iterable[Business], human_status: bool = True) -> list[dict[str, Any]]:
    rows = []
    for business in businesses:
        row = business.to_row()
        if human_status:
            row["site_status"] = STATUS_TITLES.get(row["site_status"], row["site_status"])
        rows.append(row)
    return rows


def write_csv(
    businesses: Iterable[Business],
    path: str | Path,
    delimiter: str = ";",
    fields: list[str] | None = None,
) -> int:
    """CSV с BOM и ';' — открывается в Excel по-русски без плясок с кодировкой."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = fields or ROW_FIELDS
    rows = _rows(businesses)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=columns, delimiter=delimiter, extrasaction="ignore"
        )
        writer.writerow({field: COLUMN_TITLES.get(field, field) for field in columns})
        for row in rows:
            writer.writerow({field: row[field] for field in columns})
    return len(rows)


def write_json(businesses: Iterable[Business], path: str | Path) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = [business.to_dict() for business in businesses]
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return len(payload)


def write_xlsx(
    businesses: Iterable[Business],
    path: str | Path,
    fields: list[str] | None = None,
    sheet_title: str = "Лиды",
) -> int:
    """Минимальный писатель XLSX: обычная книга с одним листом и заголовком."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    columns = fields or ROW_FIELDS
    rows = _rows(businesses)
    header = [COLUMN_TITLES.get(field, field) for field in columns]
    table = [header] + [[row[field] for field in columns] for row in rows]

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as book:
        book.writestr("[Content_Types].xml", _CONTENT_TYPES)
        book.writestr("_rels/.rels", _ROOT_RELS)
        book.writestr("xl/workbook.xml", _workbook_xml(sheet_title))
        book.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        book.writestr("xl/styles.xml", _STYLES)
        book.writestr("xl/worksheets/sheet1.xml", _sheet_xml(table, columns))
    return len(rows)


# Ширина колонок в символах: без неё всё слипается в один столбик.
_COLUMN_WIDTHS: dict[str, int] = {
    "name": 34,
    "category": 22,
    "phone": 20,
    "all_phones": 26,
    "address": 40,
    "site_status": 20,
    "url": 32,
    "site_domain": 24,
    "links": 30,
    "lead_score": 12,
    "score_reasons": 60,
    "rating": 9,
    "reviews": 10,
    "hours": 26,
    "closed": 9,
    "lon": 12,
    "lat": 12,
    "maps_url": 44,
    "source_url": 44,
    "company_id": 20,
    "query": 22,
}


def _sheet_xml(table: Sequence[Sequence[Any]], columns: Sequence[str]) -> str:
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{_COLUMN_WIDTHS.get(name, 18)}" '
        f'customWidth="1"/>'
        for i, name in enumerate(columns)
    )
    last_cell = f"{_column_name(max(0, len(columns) - 1))}{max(1, len(table))}"
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>",
        f"<cols>{cols}</cols>",
        "<sheetData>",
    ]
    for row_index, row in enumerate(table, start=1):
        cells = []
        for col_index, value in enumerate(row):
            ref = f"{_column_name(col_index)}{row_index}"
            style = ' s="1"' if row_index == 1 else ""
            if isinstance(value, bool):
                value = "да" if value else "нет"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{ref}"{style}><v>{value}</v></c>')
            else:
                text = escape(_clean(str(value)))
                cells.append(
                    f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">'
                    f"{text}</t></is></c>"
                )
        parts.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    parts.append("</sheetData>")
    parts.append(f'<autoFilter ref="A1:{last_cell}"/>')
    parts.append("</worksheet>")
    return "".join(parts)


_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(text: str) -> str:
    return _ILLEGAL_XML.sub("", text)


def _column_name(index: int) -> str:
    name = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

def _workbook_xml(sheet_title: str) -> str:
    # Excel не принимает эти символы в имени листа и режет его по 31 символу.
    title = re.sub(r"[\\/*?:\[\]]", "-", sheet_title)[:31] or "Лиды"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(title)}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
</styleSheet>"""


WRITERS = {"csv": write_csv, "json": write_json, "xlsx": write_xlsx}


def write(
    businesses: Iterable[Business],
    path: str | Path,
    fmt: str | None = None,
    *,
    compact: bool = False,
    fields: list[str] | None = None,
    sheet_title: str = "Лиды",
) -> int:
    """Пишет результат в формате, определённом явно или по расширению файла."""
    target = Path(path)
    chosen = (fmt or target.suffix.lstrip(".") or "csv").lower()
    if chosen not in WRITERS:
        raise ValueError(
            f"неизвестный формат '{chosen}', доступны: {', '.join(sorted(WRITERS))}"
        )
    if chosen == "json":
        # JSON читает программа, а не человек: колонки там не режем.
        return write_json(businesses, target)
    columns = resolve_fields(compact, fields)
    if chosen == "xlsx":
        return write_xlsx(businesses, target, fields=columns, sheet_title=sheet_title)
    return write_csv(businesses, target, fields=columns)

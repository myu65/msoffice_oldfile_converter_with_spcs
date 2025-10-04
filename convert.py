#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
convert.py (UNO内蔵版)
- 旧形式をモダン化（doc->docx, ppt->pptx, xls->xlsx）
- --passthrough-modern で docx/pptx/xlsx/odt/odp/ods をコピー
- --also-pdf で PDF 同時出力
- --calc-fit-export で Excel系（xls/xlsx/ods/csv）は UNO 経由:
    * ScaleToPagesX=1（横1ページに収める）
    * ScaleToPagesY=0（縦はLibreOffice任せ＝既存の手動改ページ尊重）
    * 方向は自動判定（列数>=行数で横向き）
  ※ レイアウトリセット・列幅変更はしません
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------- 小物 ----------
def log(msg: str):
    print(msg, flush=True)

def ensure_parent(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)

def run_lo(cmd: list[str], check: bool = True) -> int:
    log("+ " + " ".join(cmd))
    return subprocess.run(cmd, check=check).returncode

# ---------- 種別判定 ----------
def is_writer(p: Path) -> bool:
    return p.suffix.lower() in (".doc", ".docx", ".odt", ".rtf")

def is_impress(p: Path) -> bool:
    return p.suffix.lower() in (".ppt", ".pptx", ".odp")

def is_calc(p: Path) -> bool:
    return p.suffix.lower() in (".xls", ".xlsx", ".ods", ".csv")

def is_modern(p: Path) -> bool:
    return p.suffix.lower() in (".docx", ".pptx", ".xlsx", ".odt", ".odp", ".ods")

# ---------- soffice CLI ----------
FILTERS_TO_MODERN = {
    ".doc": ("docx", "MS Word 2007 XML"),
    ".ppt": ("pptx", "Impress MS PowerPoint 2007 XML"),
    ".xls": ("xlsx", "Calc MS Excel 2007 XML"),
}
PDF_FILTER = {
    "writer": "pdf:writer_pdf_Export",
    "impress": "pdf:impress_pdf_Export",
    "calc":   "pdf:calc_pdf_Export",
}

def soffice_convert(ext_and_filter: str, inputs: list[Path], outdir: Path):
    if not inputs:
        return
    cmd = [
        "soffice",
        "--headless", "--invisible", "--nologo", "--nodefault",
        "--nolockcheck", "--nocrashreport", "--nofirststartwizard",
        "--convert-to", ext_and_filter,
        "--outdir", str(outdir),
    ] + [str(x) for x in inputs]
    run_lo(cmd)

# ---------- UNO（Calc PDFだけ） ----------
#  * 列幅は触らない
#  * 改ページはリセットしない
#  * 既存の印刷範囲があれば尊重（なければ何もしない＝LibreOfficeの既定に従う）
_uno_ctx = None  # 1プロセス内で共有

def _uno_connect():
    global _uno_ctx
    if _uno_ctx is not None:
        return _uno_ctx
    try:
        import uno
        from com.sun.star.connection import NoConnectException
        local_ctx = uno.getComponentContext()
        resolver = local_ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local_ctx
        )
        try:
            _uno_ctx = resolver.resolve("uno:socket,host=127.0.0.1,port=2002;urp;StarOffice.ComponentContext")
            return _uno_ctx
        except NoConnectException:
            # 起動してから再接続
            sp = subprocess.Popen([
                "soffice", "--headless", "--invisible", "--nologo", "--nodefault",
                "--nolockcheck", "--nocrashreport", "--nofirststartwizard",
                "--accept=socket,host=127.0.0.1,port=2002;urp;"
            ])
            import time
            for _ in range(60):
                time.sleep(0.1)
                try:
                    _uno_ctx = resolver.resolve("uno:socket,host=127.0.0.1,port=2002;urp;StarOffice.ComponentContext")
                    return _uno_ctx
                except NoConnectException:
                    continue
            sp.terminate()
            raise RuntimeError("failed to start soffice UNO bridge")
    except Exception as e:
        raise RuntimeError("python3-uno が必要です（apt: python3-uno）") from e

def _colletter(idx: int) -> str:
    # 0-based -> Excel列名
    s = ""
    x = idx
    while True:
        s = chr(65 + (x % 26)) + s
        x = x // 26 - 1
        if x < 0:
            break
    return s

def _used_area(sheet):
    cur = sheet.createCursor()
    cur.gotoStartOfUsedArea(False)
    cur.gotoEndOfUsedArea(True)
    return cur.getRangeAddress()

def export_calc_pdf_via_uno(src: Path, dst_pdf: Path):
    import uno
    from com.sun.star.beans import PropertyValue

    def _prop(name, value):
        p = PropertyValue()
        p.Name = name
        p.Value = value
        return p

    ctx = _uno_connect()
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

    in_url = src.resolve().as_uri()
    doc = desktop.loadComponentFromURL(in_url, "_blank", 0, (
        _prop("Hidden", True),
        _prop("ReadOnly", True),
    ))

    try:
        sheets = doc.Sheets
        styles = doc.getStyleFamilies().getByName("PageStyles")
        for i in range(sheets.Count):
            ws = sheets.getByIndex(i)

            # 使われている領域（向き判定に使う。印刷範囲は変更しない）
            ra = _used_area(ws)
            ncols = max(1, ra.EndColumn - ra.StartColumn + 1)
            nrows = max(1, ra.EndRow - ra.StartRow + 1)

            # ページスタイル取得
            ps_name = ws.PageStyle
            ps = styles.getByName(ps_name)

            # 横方向は必ず1ページに収める、縦方向は0（LibreOffice任せ＝手動改ページ尊重）
            ps.setPropertyValue("ScaleToPagesX", 1)
            ps.setPropertyValue("ScaleToPagesY", 0)

            # 方向は自動（列>=行で横）
            ps.setPropertyValue("IsLandscape", ncols >= nrows)

        out_url = dst_pdf.resolve().as_uri()
        pdf_opts = uno.Any("[]com.sun.star.beans.PropertyValue", (
            _prop("SelectPdfVersion", 1),
            _prop("Quality", 100),
            _prop("ReduceImageResolution", False),
            _prop("UseLosslessCompression", True),
        ))
        doc.storeToURL(out_url, (
            _prop("FilterName", "calc_pdf_Export"),
            _prop("FilterData", pdf_opts),
        ))
    finally:
        try:
            doc.close(True)
        except Exception:
            pass

# ---------- 変換ルーチン ----------
def normalize_legacy_to_modern(files: list[Path], outdir: Path):
    # 旧拡張子単位でまとめて処理
    for legacy_ext, (dst_ext, filter_name) in FILTERS_TO_MODERN.items():
        batch = [p for p in files if p.suffix.lower() == legacy_ext]
        if not batch:
            continue
        soffice_convert(f"{dst_ext}:{filter_name}", batch, outdir)
        for p in batch:
            log(f"convert {p} -> {outdir / (p.stem + '.' + dst_ext)} using filter : {filter_name}")

def export_pdfs(all_files: list[Path], outdir: Path, calc_fit_export: bool):
    # Calc系/その他で分ける
    calc_files = [p for p in all_files if is_calc(p)]
    other_files = [p for p in all_files if not is_calc(p)]

    # Calc 系: UNO で1件ずつ（横1ページ・縦自由・改ページ尊重）
    if calc_fit_export and calc_files:
        for p in calc_files:
            dst = outdir / (p.stem + ".pdf")
            ensure_parent(dst)
            try:
                export_calc_pdf_via_uno(p, dst)
                log(f"convert {p} -> {dst} using UNO(calc_pdf_Export, fitX=1/Y=0)")
            except Exception as e:
                log(f"[warn] UNO export failed for {p}: {e}; fallback to CLI")
                soffice_convert(PDF_FILTER["calc"], [p], outdir)

    # Writer/Impress（と、UNO未指定のCalc）
    writer = [p for p in other_files if is_writer(p)]
    impress = [p for p in other_files if is_impress(p)]
    calc_cli = [p for p in calc_files] if not calc_fit_export else []

    if writer:
        soffice_convert(PDF_FILTER["writer"], writer, outdir)
        for p in writer:
            log(f"convert {p} -> {outdir / (p.stem + '.pdf')} using filter : writer_pdf_Export")
    if impress:
        soffice_convert(PDF_FILTER["impress"], impress, outdir)
        for p in impress:
            log(f"convert {p} -> {outdir / (p.stem + '.pdf')} using filter : impress_pdf_Export")
    if calc_cli:
        soffice_convert(PDF_FILTER["calc"], calc_cli, outdir)
        for p in calc_cli:
            log(f"convert {p} -> {outdir / (p.stem + '.pdf')} using filter : calc_pdf_Export")

# ---------- メイン ----------
def main():
    ap = argparse.ArgumentParser(description="Office normalizer & PDF exporter (UNO built-in)")
    ap.add_argument("--inputs", action="append", required=True,
                    help='glob パターン（例: "/in/**/*"）。複数可')
    ap.add_argument("--out-dir", required=True, help="出力ルート（例: /out）")
    ap.add_argument("--passthrough-modern", action="store_true",
                    help="docx/pptx/xlsx/odt/odp/ods をコピー")
    ap.add_argument("--also-pdf", action="store_true",
                    help="PDF も生成")
    ap.add_argument("--calc-fit-export", action="store_true",
                    help="Excel系はUNOで 横=1ページ/縦=自由（改ページ尊重） でPDF出力")
    args = ap.parse_args()

    out_root = Path(args.out_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    # 入力収集
    inputs: list[Path] = []
    for patt in args.inputs:
        for s in glob.glob(patt, recursive=True):
            p = Path(s)
            if p.is_file():
                inputs.append(p)
    if not inputs:
        log("[info] no input files matched.")
        return

    # モダン/レガシー分離
    modern = [p for p in inputs if is_modern(p)]
    legacy = [p for p in inputs if p not in modern]

    # モダンコピー（任意）
    if args.passthrough_modern and modern:
        for p in modern:
            dst = out_root / p.name
            ensure_parent(dst)
            shutil.copy2(p, dst)
            log(f"copy {p} -> {dst}")

    # レガシー正規化
    if legacy:
        normalize_legacy_to_modern(legacy, out_root)

    # PDF
    if args.also_pdf:
        export_pdfs(inputs, out_root, calc_fit_export=args.calc_fit_export)

    log("done.")

if __name__ == "__main__":
    main()

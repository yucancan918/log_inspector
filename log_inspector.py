import sys
import os
import traceback
import datetime

try:
    if getattr(sys, 'frozen', False):
        _log_dir = os.path.dirname(sys.executable)
    else:
        _log_dir = os.path.dirname(os.path.abspath(__file__))
except Exception:
    _log_dir = os.path.expanduser('~')
_log_path = os.path.join(_log_dir, "app_error.log")

def _excepthook(exc_type, exc_value, exc_tb):
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_log_path, "a", encoding="utf-8") as f:
            f.write(f"===== {ts} =====\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
            f.write("\n")
    except Exception:
        try:
            with open(os.path.join(os.path.expanduser('~'), "app_error.log"), "a", encoding="utf-8") as f:
                f.write(f"===== {datetime.datetime.now()} =====\n")
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
                f.write("\n")
        except Exception:
            pass
    os._exit(1)

sys.excepthook = _excepthook

import re
import csv
import shutil
from dataclasses import dataclass
from typing import List, Optional, Dict, Set
import warnings

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QFileDialog,
                             QProgressBar, QTextEdit, QPlainTextEdit, QTabWidget, QGroupBox, QCheckBox,
                             QMessageBox, QDialog, QDialogButtonBox, QRadioButton, QButtonGroup,
                             QSpinBox, QComboBox, QTimeEdit)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QRect, QSize, QTime
from PyQt6.QtGui import QFont, QPainter, QColor, QTextFormat

SUPPORTED_EXTS = [".elog", ".log", ".txt", "ALL"]


@dataclass
class SearchResultItem:
    line_no: int
    line_content: str
    filepath: str = ""


@dataclass
class RefValueItem:
    filepath: str
    value: float


@dataclass
class CsvLogItem:
    filepath: str
    line_no: int
    hit_keyword: str
    line_content: str
    extracted_val: Optional[float]


class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.line_number_area_paint_event(event)


class LineNumberPlainTextEdit(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_number_area = LineNumberArea(self)

        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.cursorPositionChanged.connect(self.highlight_current_line)

        self.update_line_number_area_width(0)
        self.highlight_current_line()

    def line_number_area_width(self):
        digits = max(1, len(str(self.blockCount())))
        space = 3 + self.fontMetrics().horizontalAdvance('9') * digits
        return space + 12

    def update_line_number_area_width(self, _):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))

    def line_number_area_paint_event(self, event):
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor("#f0f0f0"))

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                painter.setPen(QColor("#666666"))
                painter.drawText(0, int(top), self.line_number_area.width() - 8,
                                 self.fontMetrics().height(),
                                 Qt.AlignmentFlag.AlignRight, number)
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            block_number += 1

    def highlight_current_line(self):
        extra_selections = []
        if not self.isReadOnly():
            selection = QTextEdit.ExtraSelection()
            line_color = QColor(Qt.GlobalColor.yellow).lighter(160)
            selection.format.setBackground(line_color)
            selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            extra_selections.append(selection)
        self.setExtraSelections(extra_selections)


class FolderScanThread(QThread):
    sig_progress = pyqtSignal(int, int, int)
    sig_file_done = pyqtSignal(str, int)
    sig_result_batch = pyqtSignal(list)
    sig_raw_text = pyqtSignal(str, str)
    sig_stat_update = pyqtSignal(int, dict)
    sig_finished = pyqtSignal(dict, dict, list, list)
    sig_error_log = pyqtSignal(str)

    def __init__(self, folder: str, keywords: List[str], ext_filter: str,
                 selected_levels: Set[str],
                 extract_value: bool = False):
        super().__init__()
        self.folder = folder
        self.keywords = [kw for kw in keywords if kw]
        self.ext_filter = ext_filter
        self.selected_levels = selected_levels
        self.extract_value = extract_value
        self.stop_flag = False

        self.re_dict: Dict[str, re.Pattern] = {}
        self.key_patterns: Dict[str, re.Pattern] = {}
        self.level_re: Optional[re.Pattern] = None

        for kw in self.keywords:
            self.key_patterns[kw] = re.compile(re.escape(kw))
            if extract_value:
                self.re_dict[kw] = re.compile(re.escape(kw) + r".*?([+-]?(?:\d+\.\d*|\.\d+|\d+))")

        if selected_levels:
            escaped = [re.escape(l) for l in selected_levels]
            self.level_re = re.compile("|".join(escaped))

    def run(self):
        file_list = self._collect_files()
        total = len(file_list)

        if total == 0:
            self.sig_finished.emit({}, {}, [], [])
            return

        no_filter = (not self.key_patterns) and (self.level_re is None)

        file_hit_dict: Dict[str, int] = {}
        kw_hit_dict: Dict[str, int] = {kw: 0 for kw in self.keywords}
        ref_data_list: List[RefValueItem] = []
        csv_item_list: List[CsvLogItem] = []

        if no_filter:
            for idx, fp in enumerate(file_list):
                if self.stop_flag:
                    break
                try:
                    text = self._read_file_text(fp)
                    self.sig_raw_text.emit(fp, text)
                    lines = text.splitlines()
                    file_hit_dict[fp] = len(lines)
                    for ln, line in enumerate(lines, 1):
                        csv_item_list.append(CsvLogItem(
                            filepath=fp, line_no=ln, hit_keyword="",
                            line_content=line, extracted_val=None
                        ))
                    self.sig_file_done.emit(fp, file_hit_dict[fp])
                    progress = int(((idx + 1) / total) * 100)
                    if progress != 100:
                        self.sig_progress.emit(progress, idx + 1, total)
                except Exception as e:
                    self.sig_error_log.emit(f"读取失败: {fp} | {e}")
                    file_hit_dict[fp] = 0
            self.sig_progress.emit(100, total, total)
            self.sig_finished.emit(file_hit_dict, kw_hit_dict, ref_data_list, csv_item_list)
            return

        batch: List[SearchResultItem] = []
        BATCH_SIZE = 2000
        last_progress = -1
        total_hit = 0

        for idx, fp in enumerate(file_list):
            if self.stop_flag:
                break
            try:
                file_hit = self._scan_file_fast(fp, batch, csv_item_list, ref_data_list, kw_hit_dict)
                file_hit_dict[fp] = file_hit
                total_hit += file_hit

                if len(batch) >= BATCH_SIZE:
                    self.sig_result_batch.emit(batch)
                    batch = []

                self.sig_file_done.emit(fp, file_hit)
                progress = int(((idx + 1) / total) * 100)
                if progress != last_progress:
                    last_progress = progress
                    self.sig_progress.emit(progress, idx + 1, total)
                    self.sig_stat_update.emit(total_hit, dict(kw_hit_dict))
            except Exception as e:
                self.sig_error_log.emit(f"读取失败: {fp} | {e}")
                file_hit_dict[fp] = 0

        if batch:
            self.sig_result_batch.emit(batch)

        self.sig_finished.emit(file_hit_dict, kw_hit_dict, ref_data_list, csv_item_list)

    def _read_file_text(self, fp: str) -> str:
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except UnicodeDecodeError:
            with open(fp, "r", encoding="gbk", errors="ignore") as f:
                return f.read()

    def _scan_file_fast(self, fp: str, batch: List[SearchResultItem],
                   csv_item_list: List[CsvLogItem],
                   ref_data_list: List[RefValueItem],
                   kw_hit_dict: Dict[str, int]) -> int:
        hit_count = 0
        try:
            text = self._read_file_text(fp)
        except Exception:
            return 0
        lines = text.splitlines()
        total_lines = len(lines)
        i = 0
        while i < total_lines:
            if self.stop_flag:
                break
            line = lines[i]
            i += 1
            if self.level_re is not None and not self.level_re.search(line):
                continue
            if not self.key_patterns:
                hit_count += 1
                batch.append(SearchResultItem(
                    line_no=i,
                    line_content=line,
                    filepath=fp
                ))
                csv_item_list.append(CsvLogItem(
                    filepath=fp,
                    line_no=i,
                    hit_keyword="",
                    line_content=line,
                    extracted_val=None
                ))
                continue
            for kw, pat in self.key_patterns.items():
                if pat.search(line):
                    hit_count += 1
                    kw_hit_dict[kw] += 1
                    batch.append(SearchResultItem(
                        line_no=i,
                        line_content=line,
                        filepath=fp
                    ))
                    val = None
                    if self.extract_value and kw in self.re_dict:
                        m = self.re_dict[kw].search(line)
                        if m:
                            try:
                                val = float(m.group(1))
                                ref_data_list.append(RefValueItem(filepath=fp, value=val))
                            except ValueError:
                                pass
                    csv_item_list.append(CsvLogItem(
                        filepath=fp,
                        line_no=i,
                        hit_keyword=kw,
                        line_content=line,
                        extracted_val=val
                    ))
        return hit_count

    def _collect_files(self) -> List[str]:
        result = []
        for root, _, files in os.walk(self.folder):
            for fn in files:
                full = os.path.join(root, fn)
                if self.ext_filter == "ALL" or fn.lower().endswith(self.ext_filter):
                    result.append(full)
        return result

    def stop(self):
        self.stop_flag = True


_CSV_MAX_LINE_LEN = 50000
_INVALID_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')

def _sanitize_text(s: str) -> str:
    if not s:
        return ""
    s = _INVALID_CTRL_RE.sub('', s)
    for ch in s:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            s = s.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')
            break
    if len(s) > _CSV_MAX_LINE_LEN:
        s = s[:_CSV_MAX_LINE_LEN]
    return s


def _csv_row(item):
    val_str = str(item.extracted_val) if item.extracted_val is not None else ""
    return [
        _sanitize_text(item.filepath),
        item.line_no,
        _sanitize_text(item.hit_keyword),
        val_str,
        _sanitize_text(item.line_content),
    ]


def _export_csv_in_main_thread(parent, save_path, items, on_done, cancel_flag_getter=None):
    import csv as _csv
    try:
        f = open(save_path, "w", encoding="utf-8-sig", newline="")
    except Exception as e:
        QMessageBox.critical(parent, "失败", f"无法创建文件：{e}")
        on_done(False)
        return

    writer = _csv.writer(f)
    try:
        writer.writerow(["FilePath", "LineNumber", "HitKeyword", "ExtractedValue", "LogContent"])
    except Exception:
        f.close()
        on_done(False)
        return

    idx = [0]
    total = len(items)
    BATCH = 2000
    finished = [False]

    def _cancel_requested():
        if cancel_flag_getter is None:
            return False
        try:
            return bool(cancel_flag_getter())
        except Exception:
            return False

    def _cleanup_failed():
        try:
            f.close()
        except Exception:
            pass

    def do_batch():
        if finished[0]:
            return
        if _cancel_requested():
            finished[0] = True
            _cleanup_failed()
            try:
                parent.log_text.append("导出已取消")
            except Exception:
                pass
            on_done(False)
            return

        try:
            end = min(idx[0] + BATCH, total)
            for i in range(idx[0], end):
                writer.writerow(_csv_row(items[i]))
            idx[0] = end
        except Exception as e:
            finished[0] = True
            _cleanup_failed()
            try:
                parent.log_text.append(f"✗ CSV导出失败：{e}")
                QMessageBox.critical(parent, "失败", f"CSV导出失败：{e}")
            except Exception:
                pass
            on_done(False)
            return

        if finished[0] or _cancel_requested():
            finished[0] = True
            _cleanup_failed()
            on_done(False)
            return

        try:
            parent.log_text.append(f"导出进度：{idx[0]}/{total}")
        except Exception:
            pass

        if idx[0] < total:
            QTimer.singleShot(1, do_batch)
        else:
            finished[0] = True
            _cleanup_failed()
            try:
                parent.log_text.append(f"✓ CSV导出成功，共 {total} 条记录")
            except Exception:
                pass
            QMessageBox.information(parent, "完成", "CSV明细导出成功！")
            on_done(True)

    QTimer.singleShot(1, do_batch)


class AutoAnalyzeThread(QThread):
    sig_finished = pyqtSignal(bool, str, dict)

    def __init__(self, scan_path, keywords, ext_filter, selected_levels,
                 output_dir, already_copied):
        super().__init__()
        self.scan_path = scan_path
        self.keywords = keywords
        self.ext_filter = ext_filter
        self.selected_levels = selected_levels
        self.output_dir = output_dir
        self.already_copied = dict(already_copied)
        self.errors: List[str] = []
        self._file_hit_dict: Dict[str, int] = {}
        self._kw_hit_dict: Dict[str, int] = {}
        self._csv_items: List[CsvLogItem] = []
        self._stopped = False
        self._scanner = None

    def stop(self):
        self._stopped = True
        if self._scanner is not None:
            self._scanner.stop()

    def _on_scanner_finished(self, hit_dict, kw_hit_dict, ref_data_list, csv_items):
        self._file_hit_dict = hit_dict
        self._kw_hit_dict = kw_hit_dict
        self._csv_items = csv_items

    def _on_scanner_error(self, msg):
        self.errors.append(msg)

    def run(self):
        ts = datetime.datetime.now()
        ts_str = ts.strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(self.output_dir, f"auto_analysis_report_{ts_str}.txt")

        if not self.scan_path:
            self.errors.append("扫描路径为空，未执行扫描")
        elif not os.path.exists(self.scan_path):
            self.errors.append(f"扫描路径不存在：{self.scan_path}")
        else:
            is_single = os.path.isfile(self.scan_path)
            folder = os.path.dirname(self.scan_path) if is_single else self.scan_path
            self._scanner = FolderScanThread(
                folder=folder,
                keywords=self.keywords,
                ext_filter=self.ext_filter,
                selected_levels=self.selected_levels,
                extract_value=False,
            )
            if is_single:
                self._scanner._collect_files = lambda: [self.scan_path]
            self._scanner.sig_error_log.connect(
                self._on_scanner_error, Qt.ConnectionType.DirectConnection)
            self._scanner.sig_finished.connect(
                self._on_scanner_finished, Qt.ConnectionType.DirectConnection)
            try:
                self._scanner.run()
            except Exception as e:
                self.errors.append(f"扫描执行失败：{e}")

        if self._stopped:
            self.sig_finished.emit(False, report_path, {})
            return

        csv_path = ""
        if self._csv_items:
            csv_name = f"auto_analysis_detail_{ts_str}.csv"
            csv_path = os.path.join(self.output_dir, csv_name)
            try:
                self._write_csv(csv_path, self._csv_items)
            except Exception as e:
                self.errors.append(f"CSV明细导出失败：{e}")
                csv_path = ""

        try:
            copied = self._copy_hit_files(self._file_hit_dict)
        except Exception as e:
            copied = {}
            self.errors.append(f"复制日志文件阶段异常：{e}")

        try:
            self._write_report(report_path, ts, self._file_hit_dict,
                               self._kw_hit_dict, len(self._csv_items),
                               csv_path, copied)
        except Exception as e:
            self.errors.append(f"汇总报告写入失败：{e}")

        self.sig_finished.emit(True, report_path, copied)

    def _write_csv(self, path, items):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["FilePath", "LineNumber", "HitKeyword", "ExtractedValue", "LogContent"])
            for item in items:
                writer.writerow(_csv_row(item))

    def _copy_hit_files(self, file_hit_dict):
        if not os.path.isdir(self.output_dir):
            os.makedirs(self.output_dir, exist_ok=True)
        try:
            used_names = set(os.listdir(self.output_dir))
        except Exception:
            used_names = set()
        copied = {}
        for fp, cnt in file_hit_dict.items():
            if self._stopped:
                break
            if cnt <= 0:
                continue
            prev_dst = self.already_copied.get(fp)
            if prev_dst and os.path.exists(prev_dst):
                continue
            try:
                if not os.path.isfile(fp):
                    self.errors.append(f"复制失败：源文件不存在或不可读：{fp}")
                    continue
                if prev_dst:
                    dst = self._unique_dst_for_base(os.path.basename(prev_dst), used_names)
                else:
                    dst = self._unique_dst(fp, used_names)
                shutil.copy2(fp, dst)
                copied[fp] = dst
            except PermissionError as e:
                self.errors.append(f"复制失败（权限不足）：{fp} | {e}")
            except OSError as e:
                self.errors.append(f"复制失败：{fp} | {e}")
            except Exception as e:
                self.errors.append(f"复制失败：{fp} | {e}")
        return copied

    def _unique_dst(self, src, used_names):
        return self._unique_dst_for_base(os.path.basename(src), used_names)

    def _unique_dst_for_base(self, base, used_names):
        if base not in used_names:
            used_names.add(base)
            return os.path.join(self.output_dir, base)
        stem, ext = os.path.splitext(base)
        i = 2
        while True:
            candidate = f"{stem}_{i}{ext}"
            if candidate not in used_names:
                used_names.add(candidate)
                return os.path.join(self.output_dir, candidate)
            i += 1

    def _write_report(self, path, ts, file_hit_dict, kw_hit_dict,
                      csv_total, csv_path, copied):
        lines = []
        lines.append("=" * 64)
        lines.append("【汇总报告】")
        lines.append("=" * 64)
        lines.append(f"扫描时间：{ts.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"扫描路径：{self.scan_path}")
        lines.append(f"文件后缀类型：{self.ext_filter}")
        level_str = "、".join(sorted(self.selected_levels)) if self.selected_levels else "(不限制)"
        lines.append(f"日志等级筛选：{level_str}")
        kw_str = "、".join(self.keywords) if self.keywords else "(空，不限制关键字)"
        lines.append(f"搜索关键字：{kw_str}")
        lines.append("")
        lines.append("-" * 64)
        lines.append("命中文件清单：")
        hit_files = [(fp, cnt) for fp, cnt in file_hit_dict.items() if cnt > 0]
        if hit_files:
            for i, (fp, cnt) in enumerate(hit_files, 1):
                lines.append(f"  {i}. {fp} | 命中 {cnt} 条")
        else:
            lines.append("  (无命中文件)")
        lines.append("")
        lines.append("-" * 64)
        lines.append("各关键字命中统计：")
        if kw_hit_dict:
            for kw, cnt in kw_hit_dict.items():
                lines.append(f"  关键字【{kw}】：{cnt} 条")
        else:
            lines.append("  (无关键字筛选)")
        lines.append("")
        lines.append("-" * 64)
        lines.append(f"CSV明细文件：{csv_path if csv_path else '(未生成)'}")
        lines.append(f"命中记录总数：{csv_total} 条")
        lines.append("")
        lines.append("-" * 64)
        lines.append("复制日志文件：")
        if copied:
            for fp in copied:
                lines.append(f"  {fp}")
        else:
            lines.append("  (无新复制文件)")
        lines.append("")
        lines.append("-" * 64)
        lines.append("异常信息：")
        if self.errors:
            for e in self.errors:
                lines.append(f"  ✗ {e}")
        else:
            lines.append("  (无)")
        lines.append("")
        lines.append("=" * 64)
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(lines))


class FilterExportThread(QThread):
    sig_log = pyqtSignal(str)
    sig_finished = pyqtSignal(bool, int, int)

    def __init__(self, src_path, out_path, filter_keys, is_dir):
        super().__init__()
        self.src_path = src_path
        self.out_path = out_path
        if isinstance(filter_keys, str):
            filter_keys = [k.strip() for k in filter_keys.replace("，", ",").split(",") if k.strip()]
        self.filter_keys = filter_keys
        self.is_dir = is_dir
        self.stop_flag = False

    def _read_text(self, fp):
        for enc in ("utf-8", "gbk"):
            try:
                with open(fp, "r", encoding=enc, errors="ignore") as f:
                    return f.read()
            except Exception:
                continue
        return ""

    def _process_file(self, src, dst):
        text = self._read_text(src)
        lines = text.splitlines(keepends=True)
        kept = []
        removed = 0
        if not self.filter_keys:
            kept = lines
        else:
            for ln in lines:
                if any(k in ln for k in self.filter_keys):
                    removed += 1
                else:
                    kept.append(ln)
        if kept:
            d = os.path.dirname(dst)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(dst, "w", encoding="utf-8-sig", newline="") as f:
                f.writelines(kept)
        return len(lines), removed

    def run(self):
        total_lines = 0
        total_removed = 0
        file_cnt = 0
        try:
            if not self.is_dir:
                if not os.path.isfile(self.src_path):
                    self.sig_log.emit(f"源文件不存在：{self.src_path}")
                    self.sig_finished.emit(False, 0, 0)
                    return
                os.makedirs(self.out_path, exist_ok=True)
                dst = os.path.join(self.out_path, os.path.basename(self.src_path))
                self.sig_log.emit(f"[单文件模式] {self.src_path}")
                self.sig_log.emit(f"  → {dst}")
                try:
                    cnt, rm = self._process_file(self.src_path, dst)
                    total_lines, total_removed, file_cnt = cnt, rm, 1
                    self.sig_log.emit(f"  总行 {cnt} | 过滤 {rm} | 保留 {cnt - rm}")
                except Exception as e:
                    self.sig_log.emit(f"  处理失败：{e}")
                    self.sig_finished.emit(False, 0, 0)
                    return
            else:
                if not os.path.isdir(self.src_path):
                    self.sig_log.emit(f"源目录不存在：{self.src_path}")
                    self.sig_finished.emit(False, 0, 0)
                    return
                os.makedirs(self.out_path, exist_ok=True)
                for root, _, files in os.walk(self.src_path):
                    for fn in files:
                        if self.stop_flag:
                            self.sig_log.emit("已停止")
                            self.sig_finished.emit(False, total_lines, file_cnt)
                            return
                        src = os.path.join(root, fn)
                        rel = os.path.relpath(src, self.src_path)
                        dst = os.path.join(self.out_path, rel)
                        try:
                            cnt, rm = self._process_file(src, dst)
                            total_lines += cnt
                            total_removed += rm
                            file_cnt += 1
                            self.sig_log.emit(f"  {rel}  ({cnt} 行)")
                        except Exception as e:
                            self.sig_log.emit(f"  ✗ {rel}: {e}")

            keys_str = ",".join(self.filter_keys) if self.filter_keys else "(空)"
            self.sig_log.emit(f"✓ 完成 | 文件 {file_cnt} | 总行 {total_lines} | 过滤 {total_removed} | 关键字: {keys_str}")
            self.sig_finished.emit(True, total_lines, file_cnt)
        except Exception as e:
            self.sig_log.emit(f"导出失败：{e}")
            self.sig_finished.emit(False, total_lines, file_cnt)

    def stop(self):
        self.stop_flag = True


class LogScanDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("文件夹批量扫描：多关键字计数 + 关键字数值绘图")
        self.resize(900, 750)
        self.setAcceptDrops(True)

        self.scan_thread: Optional[FolderScanThread] = None
        self.file_hit_result: Dict[str, int] = {}
        self.kw_hit_result: Dict[str, int] = {}
        self.all_ref_data: List[RefValueItem] = []
        self.csv_hit_items: List[CsvLogItem] = []
        self._csv_exporting = False
        self._csv_export_cancel = False

        self.fig = None
        self.canvas = None
        self.toolbar = None
        self.ax = None
        self.current_page = 0
        self.total_page = 0
        self.page_size = 3000

        warnings.filterwarnings("ignore", category=UserWarning, message=".*Glyph.*")
        import matplotlib
        matplotlib.use("QtAgg")
        matplotlib.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
        matplotlib.rcParams['axes.unicode_minus'] = False
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas

        self.Figure = Figure
        self.FigureCanvas = FigureCanvas
        self.NavigationToolbar2QT = NavigationToolbar

        self.init_ui()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1:
            path = urls[0].toLocalFile()
            self.edit_folder.setText(path)
        event.acceptProposedAction()

    def current_ext(self) -> str:
        return self.combo_ext.currentText()

    def init_ui(self):
        vl = QVBoxLayout(self)

        row_folder = QHBoxLayout()
        row_folder.addWidget(QLabel("扫描路径："))
        self.edit_folder = QLineEdit()
        self.edit_folder.setPlaceholderText("支持拖拽文件夹到此处输入")
        btn_browse_folder = QPushButton("选择文件夹")
        btn_browse_folder.clicked.connect(self.select_folder)
        row_folder.addWidget(self.edit_folder)
        row_folder.addWidget(btn_browse_folder)
        vl.addLayout(row_folder)

        row_ext = QHBoxLayout()
        row_ext.addWidget(QLabel("文件后缀类型："))
        self.combo_ext = QComboBox()
        self.combo_ext.addItems(SUPPORTED_EXTS)
        self.combo_ext.setCurrentText(".elog")
        row_ext.addWidget(self.combo_ext)
        row_ext.addWidget(QLabel("ALL 表示不限后缀"))
        row_ext.addStretch()
        vl.addLayout(row_ext)

        row_key = QHBoxLayout()
        row_key.addWidget(QLabel("搜索关键字："))
        self.edit_key = QLineEdit()
        self.edit_key.setPlaceholderText("多个关键字用英文逗号分隔，例如：A,B,C")
        row_key.addWidget(self.edit_key)
        vl.addLayout(row_key)

        row_page_size = QHBoxLayout()
        row_page_size.addWidget(QLabel("单页大绘图点数："))
        self.spin_page_size = QSpinBox()
        self.spin_page_size.setRange(100, 50000)
        self.spin_page_size.setValue(3000)
        self.spin_page_size.setSingleStep(500)
        self.spin_page_size.valueChanged.connect(self.on_page_size_changed)
        row_page_size.addWidget(self.spin_page_size)
        row_page_size.addWidget(QLabel("建议：低配电脑使用5000，高配电脑可50000"))
        row_page_size.addStretch()
        vl.addLayout(row_page_size)

        group_mode = QGroupBox("扫描模式")
        mode_layout = QHBoxLayout(group_mode)
        self.radio_count_only = QRadioButton("① 仅统计关键字出现次数（速度快）")
        self.radio_count_plot = QRadioButton("② 统计次数 + 提取关键字数值绘图")
        self.radio_count_only.setChecked(True)
        self.btn_group_mode = QButtonGroup()
        self.btn_group_mode.addButton(self.radio_count_only)
        self.btn_group_mode.addButton(self.radio_count_plot)
        mode_layout.addWidget(self.radio_count_only)
        mode_layout.addWidget(self.radio_count_plot)
        vl.addWidget(group_mode)

        row_btn = QHBoxLayout()
        self.btn_start = QPushButton("开始扫描")
        self.btn_start.clicked.connect(self.start_scan)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.clicked.connect(self.stop_scan)
        self.btn_max = QPushButton("最大化窗口")
        self.btn_max.clicked.connect(self.showMaximized)
        self.btn_export_csv = QPushButton("导出CSV明细")
        self.btn_export_csv.clicked.connect(self.export_csv_result)
        self.btn_export_csv.setEnabled(False)
        row_btn.addWidget(self.btn_start)
        row_btn.addWidget(self.btn_stop)
        row_btn.addWidget(self.btn_max)
        row_btn.addWidget(self.btn_export_csv)
        row_btn.addStretch()
        vl.addLayout(row_btn)

        self.progress_scan = QProgressBar()
        vl.addWidget(QLabel("扫描进度"))
        vl.addWidget(self.progress_scan)

        group_stat = QGroupBox("扫描结果")
        stat_layout = QVBoxLayout(group_stat)
        self.label_stat = QLabel("等待扫描...")
        stat_layout.addWidget(self.label_stat)
        vl.addWidget(group_stat)

        page_layout = QHBoxLayout()
        self.btn_prev_page = QPushButton("上一页")
        self.btn_prev_page.clicked.connect(self.draw_prev_page)
        self.btn_next_page = QPushButton("下一页")
        self.btn_next_page.clicked.connect(self.draw_next_page)
        self.label_page_info = QLabel("分页 0/0")
        self.btn_prev_page.setEnabled(False)
        self.btn_next_page.setEnabled(False)
        page_layout.addWidget(self.btn_prev_page)
        page_layout.addWidget(self.label_page_info)
        page_layout.addWidget(self.btn_next_page)
        vl.addLayout(page_layout)

        self.plot_widget = QWidget()
        self.plot_layout = QVBoxLayout(self.plot_widget)
        self.plot_widget.setMinimumHeight(300)
        vl.addWidget(self.plot_widget)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 10))
        vl.addWidget(self.log_text)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        vl.addWidget(btns)

    def select_folder(self):
        p = QFileDialog.getExistingDirectory(self, "选择扫描根文件夹")
        if p:
            self.edit_folder.setText(p)

    def create_plot_canvas(self):
        if self.fig is not None:
            self.fig.clf()
            self.plot_layout.removeWidget(self.canvas)
            self.canvas.deleteLater()
            self.toolbar.deleteLater()

        self.fig = self.Figure(figsize=(10, 4), dpi=100)
        self.canvas = self.FigureCanvas(self.fig)
        self.toolbar = self.NavigationToolbar2QT(self.canvas, self.plot_widget)
        self.plot_layout.addWidget(self.toolbar)
        self.plot_layout.addWidget(self.canvas)
        self.ax = self.fig.add_subplot(111)
        self.canvas.mpl_connect('pick_event', self.on_pick_event)
        self.clear_plot()

    def on_page_size_changed(self, val):
        self.page_size = val

    def clear_plot(self):
        if self.ax is None:
            return
        self.ax.clear()
        self.ax.set_title("等待扫描数据...")
        self.canvas.draw()

    def on_pick_event(self, event):
        if not event.artist or len(event.ind) == 0:
            return
        idx_in_page = event.ind[0]
        start_idx = self.current_page * self.page_size
        global_idx = start_idx + idx_in_page
        if global_idx >= len(self.all_ref_data):
            return
        item = self.all_ref_data[global_idx]
        short_name = os.path.basename(item.filepath)
        self.ax.set_title(f"数值曲线 | 点序号:{global_idx+1} | 值:{item.value:.4f} | 文件:{short_name}")
        self.canvas.draw_idle()

    def render_all_pages(self, ref_data: List[RefValueItem]):
        self.all_ref_data = ref_data
        self.current_page = 0
        total_cnt = len(ref_data)
        if total_cnt <= 0:
            if self.canvas is None:
                self.create_plot_canvas()
            self.clear_plot()
            self.label_page_info.setText("分页 0/0")
            self.btn_prev_page.setEnabled(False)
            self.btn_next_page.setEnabled(False)
            return

        if self.canvas is None:
            self.create_plot_canvas()

        self.total_page = (total_cnt + self.page_size - 1) // self.page_size
        self.label_page_info.setText(f"分页：{self.current_page + 1}/{self.total_page}")
        self.btn_prev_page.setEnabled(self.current_page > 0)
        self.btn_next_page.setEnabled(self.current_page < self.total_page - 1)
        self.draw_current_page()

    def draw_current_page(self):
        self.ax.clear()
        start_idx = self.current_page * self.page_size
        end_idx = start_idx + self.page_size
        page_data = self.all_ref_data[start_idx:end_idx]
        page_x = [start_idx + i + 1 for i in range(len(page_data))]
        page_y = [item.value for item in page_data]
        count = len(page_x)
        if count == 0:
            self.ax.set_title("当前页面无数据")
            self.canvas.draw()
            return

        self.ax.plot(page_x, page_y, color="#1f77b4", alpha=0.7, linewidth=1.2)
        self.ax.scatter(page_x, page_y, marker=".", color="#1f77b4", picker=True, pickradius=6)

        y_min = min(page_y)
        y_max = max(page_y)
        if abs(y_max - y_min) < 1e-9:
            offset = abs(y_min) * 0.02 if abs(y_min) > 1e-9 else 0.01
            y_min -= offset
            y_max += offset

        self.ax.set_xlim(page_x[0], page_x[-1])
        self.ax.set_ylim(y_min, y_max)
        self.ax.set_xlabel("数据序号")
        self.ax.set_ylabel("数值")
        self.ax.set_title(f"数值曲线，当前页点数：{count}，全部有效点数：{len(self.all_ref_data)}")
        self.ax.grid(True, alpha=0.3)
        self.canvas.draw()

    def draw_prev_page(self):
        if self.current_page > 0:
            self.current_page -= 1
            self.draw_current_page()
            self.label_page_info.setText(f"分页：{self.current_page+1}/{self.total_page}")
            self.btn_prev_page.setEnabled(self.current_page > 0)
            self.btn_next_page.setEnabled(self.current_page < self.total_page - 1)

    def draw_next_page(self):
        if self.current_page < self.total_page - 1:
            self.current_page += 1
            self.draw_current_page()
            self.label_page_info.setText(f"分页：{self.current_page+1}/{self.total_page}")
            self.btn_prev_page.setEnabled(self.current_page > 0)
            self.btn_next_page.setEnabled(self.current_page < self.total_page - 1)

    def export_csv_result(self):
        if not self.csv_hit_items:
            QMessageBox.warning(self, "提示", "没有导出的命中记录")
            return
        if self._csv_exporting:
            QMessageBox.information(self, "提示", "正在导出中，请稍候")
            return

        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存CSV文件",
            f"multi_keyword_log_result.csv",
            "CSV Files (*.csv)"
        )
        if not save_path:
            return

        self._csv_exporting = True
        self._csv_export_cancel = False
        self.log_text.append(f"开始导出CSV，记录数：{len(self.csv_hit_items)}")
        self.btn_export_csv.setEnabled(False)

        def on_done(ok):
            self._csv_exporting = False
            self.btn_export_csv.setEnabled(True)

        _export_csv_in_main_thread(self, save_path, self.csv_hit_items, on_done,
                                    cancel_flag_getter=lambda: self._csv_export_cancel)

    def on_csv_export_finished(self, ok):
        pass

    def start_scan(self):
        path = self.edit_folder.text().strip()
        key_raw = self.edit_key.text().strip()
        ext = self.current_ext()
        if not path:
            QMessageBox.warning(self, "提示", "请选择文件夹路径！")
            return
        if not os.path.isdir(path):
            QMessageBox.warning(self, "提示", "请选择有效的文件夹路径")
            return
        if self.scan_thread is not None and self.scan_thread.isRunning():
            return

        keywords = [k.strip() for k in key_raw.split(",") if k.strip()]

        selected_levels = set()
        self.file_hit_result.clear()
        self.kw_hit_result.clear()
        self.all_ref_data.clear()
        self.csv_hit_items.clear()
        self.log_text.clear()
        self.label_stat.setText("正在扫描...")
        self.progress_scan.setValue(0)
        self.clear_plot()
        self.current_page = 0
        self.total_page = 0
        self.btn_prev_page.setEnabled(False)
        self.btn_next_page.setEnabled(False)
        self.label_page_info.setText("分页 0/0")
        self.btn_export_csv.setEnabled(False)

        need_plot = self.radio_count_plot.isChecked()
        self.scan_thread = FolderScanThread(
            folder=path, keywords=keywords, ext_filter=ext,
            selected_levels=selected_levels,
            extract_value=need_plot
        )
        self.scan_thread.sig_progress.connect(lambda p, c, t: self.progress_scan.setValue(p))
        self.scan_thread.sig_file_done.connect(self.on_file_done)
        self.scan_thread.sig_error_log.connect(self.log_text.append)
        self.scan_thread.sig_finished.connect(self.on_all_finished)
        self.scan_thread.start()

    def stop_scan(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.stop()
            self.scan_thread.wait()
            self.log_text.append("扫描任务已停止")

    def on_file_done(self, fp: str, cnt: int):
        self.file_hit_result[fp] = cnt

    def on_all_finished(self, hit_dict, kw_hit_dict, ref_data_list, csv_items):
        self.file_hit_result = hit_dict
        self.kw_hit_result = kw_hit_dict
        self.csv_hit_items = csv_items
        total_hit_all_files = sum(hit_dict.values())

        warn_flag = total_hit_all_files >= 100000
        big_flag = total_hit_all_files >= 50000

        if self.radio_count_only.isChecked():
            msg_lines = ["【仅次数统计完成】各关键字命中统计："]
            for kw, cnt in kw_hit_dict.items():
                msg_lines.append(f"关键字【{kw}】命中：{cnt}")
            msg_lines.append(f"扫描文件总数：{len(hit_dict)}")
            if warn_flag:
                msg_lines.append("⚠️⚠️⚠️ 命中量超大，UI显示明细会严重卡顿，建议直接导出CSV查看")
            elif big_flag:
                msg_lines.append("⚠️ 命中量较大，UI可能卡顿，建议导出CSV查看")
            txt = "\n".join(msg_lines)
            self.label_stat.setText(txt)

            self.log_text.append("===== 各关键字命中汇总 =====")
            for kw, cnt in kw_hit_dict.items():
                self.log_text.append(f"关键字【{kw}】：{cnt}")
            self.log_text.append("===== 各文件全部命中汇总 =====")
            lines = [f"{fname}  | 全部命中合计：{cnt}" for fname, cnt in hit_dict.items()]
            self.log_text.append("\n".join(lines))

            if len(csv_items) > 0:
                self.btn_export_csv.setEnabled(True)
                self.log_text.append(f"✓ 命中明细共 {len(csv_items)} 条，可以点击【导出CSV明细】保存")
            else:
                self.btn_export_csv.setEnabled(False)
                self.log_text.append("扫描没有命中记录，无法导出CSV")

            if warn_flag:
                QMessageBox.critical(self, "⚠️ 严重警告",
                    f"扫描完成，命中 {total_hit_all_files} 条数据，非常大！\n\n"
                    f"UI 显示明细会导致界面长时间未响应\n"
                    f"请使用【导出CSV明细】功能保存结果后用 Excel 查看")
            elif big_flag:
                QMessageBox.warning(self, "⚠️ 警告",
                    f"扫描完成，命中 {total_hit_all_files} 条数据，较大。\n\n"
                    f"UI 可能会有明显卡顿，建议导出 CSV 后查看")
        else:
            val_count = len(ref_data_list)
            if val_count > 0:
                txt_lines = ["【统计+绘图模式完成】各关键字命中："]
                for kw, cnt in kw_hit_dict.items():
                    txt_lines.append(f"{kw}:{cnt}")
                txt_lines.append(f"成功提取到有效数值数量：{val_count}")
                if val_count >= 50000:
                    txt_lines.append("⚠️ 有效数据点较大，翻页渲染可能较慢")
                txt = "\n".join(txt_lines)
                self.label_stat.setText(txt)
                self.log_text.append(f"提取到 {val_count} 个有效数值，开始渲染图表")
                self.render_all_pages(ref_data_list)
                if val_count >= 50000:
                    QMessageBox.warning(self, "⚠️ 提示",
                        f"有效数据点 {val_count} 较大，翻页渲染图表会有明显延迟\n\n"
                        f"可以适当调大右下角的【单页最大绘图点数】或使用 CSV 导出")
            else:
                txt_lines = ["【统计+绘图模式完成】各关键字命中："]
                for kw, cnt in kw_hit_dict.items():
                    txt_lines.append(f"{kw}:{cnt}")
                txt_lines.append("⚠️ 没有找到对应格式的数字，无法绘图")
                txt = "\n".join(txt_lines)
                self.label_stat.setText(txt)
                self.clear_plot()
            self.btn_export_csv.setEnabled(False)

    def closeEvent(self, event):
        self.stop_scan()
        self._csv_export_cancel = True
        if self.fig is not None:
            self.fig.clf()
            self.canvas.deleteLater()
            self.toolbar.deleteLater()
        self.fig = None
        self.canvas = None
        self.toolbar = None
        self.ax = None
        event.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("日志分析工具 V2.2 ")
        self.resize(1150, 860)
        self.search_results: List[SearchResultItem] = []
        self.scan_thread: Optional[FolderScanThread] = None
        self.filter_thread: Optional[FilterExportThread] = None
        self.csv_hit_items: List[CsvLogItem] = []
        self._csv_exporting = False
        self._csv_export_cancel = False
        self._batch_dlg = None

        self.auto_thread: Optional[AutoAnalyzeThread] = None
        self.auto_copied_files: Dict[str, str] = {}
        self.schedule_time: Optional[QTime] = None
        self.schedule_active = False
        self.schedule_timer = QTimer(self)
        self.schedule_timer.setInterval(1000)
        self.schedule_timer.timeout.connect(self._on_schedule_tick)

        self.find_index = 0
        self.last_find_text = ""
        self.find_busy = False
        self._ui_buf: List[str] = []

        self.setAcceptDrops(True)
        self.init_ui()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1:
            path = urls[0].toLocalFile()
            self.edit_path.setText(path)
        event.acceptProposedAction()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        group_file = QGroupBox("文件夹/日志文件分析（支持拖拽到窗口）")
        layout_file = QHBoxLayout(group_file)
        layout_file.addWidget(QLabel("扫描路径："))
        self.edit_path = QLineEdit()
        self.edit_path.setPlaceholderText("选择文件夹批量扫描，或选单文件，也可拖拽")
        btn_select_folder = QPushButton("选择文件夹")
        btn_select_folder.clicked.connect(self.select_folder)
        btn_select_file = QPushButton("选择单文件")
        btn_select_file.clicked.connect(self.select_single_file)
        self.btn_batch = QPushButton("快速筛选分析+绘图")
        self.btn_batch.clicked.connect(self.open_batch_dialog)
        layout_file.addWidget(self.edit_path)
        layout_file.addWidget(btn_select_folder)
        layout_file.addWidget(btn_select_file)
        layout_file.addWidget(self.btn_batch)
        main_layout.addWidget(group_file)

        group_search = QGroupBox("扫描配置")
        layout_search = QVBoxLayout(group_search)

        row_ext = QHBoxLayout()
        row_ext.addWidget(QLabel("文件后缀类型："))
        self.combo_ext = QComboBox()
        self.combo_ext.addItems(SUPPORTED_EXTS)
        self.combo_ext.setCurrentText("ALL")
        row_ext.addWidget(self.combo_ext)
        row_ext.addStretch()
        layout_search.addLayout(row_ext)

        row_level = QHBoxLayout()
        row_level.addWidget(QLabel("日志等级筛选(多选，不勾选=不限制)："))
        self.chk_debug = QCheckBox("Debug")
        self.chk_info = QCheckBox("Info")
        self.chk_notice = QCheckBox("Notice")
        self.chk_warn = QCheckBox("Warn")
        self.chk_error = QCheckBox("Error")
        self.chk_fatal = QCheckBox("Fatal")
        row_level.addWidget(self.chk_debug)
        row_level.addWidget(self.chk_info)
        row_level.addWidget(self.chk_notice)
        row_level.addWidget(self.chk_warn)
        row_level.addWidget(self.chk_error)
        row_level.addWidget(self.chk_fatal)
        row_level.addStretch()
        layout_search.addLayout(row_level)

        row_key = QHBoxLayout()
        row_key.addWidget(QLabel("关键字(英文逗号分隔)："))
        self.edit_keyword = QLineEdit()
        self.edit_keyword.setPlaceholderText("示例：error,fail,critical")
        self.btn_start_scan = QPushButton("开始扫描")
        self.btn_start_scan.clicked.connect(self.start_scan)
        self.btn_stop_scan = QPushButton("停止")
        self.btn_stop_scan.clicked.connect(self.stop_scan)
        self.btn_stop_scan.setEnabled(False)
        row_key.addWidget(self.edit_keyword)
        row_key.addWidget(self.btn_start_scan)
        row_key.addWidget(self.btn_stop_scan)
        layout_search.addLayout(row_key)
        main_layout.addWidget(group_search)

        group_auto = QGroupBox("自动分析 / 定时任务")
        auto_layout = QVBoxLayout(group_auto)

        row_manual = QHBoxLayout()
        self.btn_auto_analyze = QPushButton("自动分析")
        self.btn_auto_analyze.clicked.connect(self.auto_analyze_manual)
        self.label_auto_status = QLabel("自动分析结果（txt / CSV / 日志文件）将保存到程序同级目录")
        self.label_auto_status.setWordWrap(True)
        row_manual.addWidget(self.btn_auto_analyze)
        row_manual.addWidget(self.label_auto_status, 1)
        auto_layout.addLayout(row_manual)

        row_schedule = QHBoxLayout()
        row_schedule.addWidget(QLabel("定时执行时间："))
        self.edit_schedule_time = QTimeEdit()
        self.edit_schedule_time.setDisplayFormat("HH:mm:ss")
        self.edit_schedule_time.setTime(QTime(8, 30))
        self.btn_schedule_start = QPushButton("启动定时")
        self.btn_schedule_start.clicked.connect(self.start_schedule)
        self.btn_schedule_stop = QPushButton("取消定时")
        self.btn_schedule_stop.clicked.connect(self.stop_schedule)
        self.btn_schedule_stop.setEnabled(False)
        self.label_countdown = QLabel("下次执行倒计时：--:--:--")
        row_schedule.addWidget(self.edit_schedule_time)
        row_schedule.addWidget(self.btn_schedule_start)
        row_schedule.addWidget(self.btn_schedule_stop)
        row_schedule.addWidget(self.label_countdown)
        auto_layout.addLayout(row_schedule)

        main_layout.addWidget(group_auto)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        main_layout.addWidget(self.progress_bar)

        tab = QTabWidget()
        result_widget = QWidget()
        result_layout = QVBoxLayout(result_widget)
        find_layout = QHBoxLayout()
        find_layout.addWidget(QLabel("页面内查找："))
        self.edit_find = QLineEdit()
        self.edit_find.setPlaceholderText("在当前结果中查找文本")
        self.btn_find_next = QPushButton("查找下一个")
        self.btn_find_next.clicked.connect(self.find_next)
        self.btn_find_prev = QPushButton("查找上一个")
        self.btn_find_prev.clicked.connect(self.find_prev)
        find_layout.addWidget(self.edit_find)
        find_layout.addWidget(self.btn_find_next)
        find_layout.addWidget(self.btn_find_prev)
        result_layout.addLayout(find_layout)

        self.tab_result = LineNumberPlainTextEdit()
        self.tab_result.setReadOnly(True)
        self.tab_result.setFont(QFont("Consolas", 10))
        self.tab_result.setMaximumBlockCount(0)
        result_layout.addWidget(self.tab_result)

        row_export = QHBoxLayout()
        self.label_stat = QLabel("等待扫描...")
        self.btn_export_csv = QPushButton("导出CSV明细")
        self.btn_export_csv.clicked.connect(self.export_csv_result)
        self.btn_export_csv.setEnabled(False)
        row_export.addWidget(self.label_stat)
        row_export.addStretch()
        row_export.addWidget(self.btn_export_csv)
        result_layout.addLayout(row_export)

        tab.addTab(result_widget, "搜索结果")

        filter_widget = QWidget()
        filter_layout = QVBoxLayout(filter_widget)
        hf1 = QHBoxLayout()
        hf1.addWidget(QLabel("过滤关键字（逗号分隔，命中该行直接删除）："))
        self.edit_filter_key = QLineEdit()
        self.edit_filter_key.setPlaceholderText("支持多个，例如：error,fail,critical")
        hf1.addWidget(self.edit_filter_key)
        filter_layout.addLayout(hf1)

        tip_row = QLabel("💡 源路径自动使用主页面【扫描路径】，单文件/目录模式自动识别")
        tip_row.setStyleSheet("color: #666; font-size: 12px;")
        filter_layout.addWidget(tip_row)

        hf2 = QHBoxLayout()
        self.btn_out = QPushButton("输出目录")
        self.btn_out.clicked.connect(self.select_filter_out)
        self.edit_filter_out = QLineEdit()
        self.edit_filter_out.setPlaceholderText("输出到哪个文件夹（保持原目录结构）")
        self.btn_run_filter = QPushButton("▶ 执行过滤导出")
        self.btn_run_filter.clicked.connect(self.run_filter_export)
        self.btn_stop_filter = QPushButton("■ 停止")
        self.btn_stop_filter.clicked.connect(self.stop_filter_export)
        self.btn_stop_filter.setEnabled(False)
        hf2.addWidget(self.btn_out)
        hf2.addWidget(self.edit_filter_out, 1)
        hf2.addWidget(self.btn_run_filter)
        hf2.addWidget(self.btn_stop_filter)
        filter_layout.addLayout(hf2)

        self.progress_filter = QProgressBar()
        self.progress_filter.setRange(0, 0)
        self.progress_filter.setVisible(False)
        filter_layout.addWidget(self.progress_filter)

        self.filter_log = LineNumberPlainTextEdit()
        self.filter_log.setReadOnly(True)
        self.filter_log.setFont(QFont("Consolas", 10))
        filter_layout.addWidget(self.filter_log)
        tab.addTab(filter_widget, "行过滤&导出")
        main_layout.addWidget(tab)

    def open_batch_dialog(self):
        if self._batch_dlg is None:
            self._batch_dlg = LogScanDialog(self)
            self._batch_dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            self._batch_dlg.destroyed.connect(lambda _obj: setattr(self, "_batch_dlg", None))
            self._batch_dlg.setWindowFlags(self._batch_dlg.windowFlags() | Qt.WindowType.Window)
        current_path = self.edit_path.text().strip()
        if current_path and not self._batch_dlg.edit_folder.text():
            self._batch_dlg.edit_folder.setText(current_path)
        self._batch_dlg.show()
        self._batch_dlg.raise_()
        self._batch_dlg.activateWindow()

    def select_folder(self):
        p = QFileDialog.getExistingDirectory(self, "选择扫描根文件夹")
        if p:
            self.edit_path.setText(p)

    def select_single_file(self):
        fp, _ = QFileDialog.getOpenFileName(self, "选择日志文件", "", "Log Files (*.log *.txt *.elog);;All Files(*)")
        if fp:
            self.edit_path.setText(fp)

    def current_levels(self) -> Set[str]:
        levels = set()
        if self.chk_debug.isChecked(): levels.add("Debug")
        if self.chk_info.isChecked(): levels.add("Info")
        if self.chk_notice.isChecked(): levels.add("Notice")
        if self.chk_warn.isChecked(): levels.add("Warn")
        if self.chk_error.isChecked(): levels.add("Error")
        if self.chk_fatal.isChecked(): levels.add("Fatal")
        return levels

    def auto_analyze_manual(self):
        self._start_auto_analysis(silent=False)

    def _start_auto_analysis(self, silent=False):
        if self.auto_thread is not None and self.auto_thread.isRunning():
            self.label_auto_status.setText("自动分析正在执行中，请稍候...")
            return

        path = self._clean_path(self.edit_path.text())
        keywords = [k.strip() for k in self.edit_keyword.text().split(",") if k.strip()]
        selected_levels = self.current_levels()
        ext = self.combo_ext.currentText()

        self.auto_thread = AutoAnalyzeThread(
            scan_path=path,
            keywords=keywords,
            ext_filter=ext,
            selected_levels=selected_levels,
            output_dir=_log_dir,
            already_copied=self.auto_copied_files,
        )
        self.auto_thread.sig_finished.connect(self._on_auto_finished)
        self.auto_thread.finished.connect(self._on_auto_thread_finished)
        self.auto_thread.start()

        if silent:
            self.label_auto_status.setText(
                f"定时自动分析已触发：{datetime.datetime.now().strftime('%H:%M:%S')}")
        else:
            self.label_auto_status.setText("自动分析执行中...")

    def _on_auto_finished(self, ok, report_path, copied):
        self.auto_copied_files.update(copied)
        if ok:
            self.label_auto_status.setText(
                f"自动分析完成，报告：{os.path.basename(report_path)}")
        else:
            self.label_auto_status.setText("自动分析已停止")

    def _on_auto_thread_finished(self):
        self.auto_thread = None

    def start_schedule(self):
        self.schedule_time = self.edit_schedule_time.time()
        self.schedule_active = True
        self.btn_schedule_start.setEnabled(False)
        self.btn_schedule_stop.setEnabled(True)
        self.schedule_timer.start()
        self._update_countdown()
        self.label_auto_status.setText("定时任务已启动，到达设定时间后自动执行")

    def stop_schedule(self):
        self.schedule_active = False
        self.schedule_time = None
        self.schedule_timer.stop()
        self.btn_schedule_start.setEnabled(True)
        self.btn_schedule_stop.setEnabled(False)
        self.label_countdown.setText("下次执行倒计时：--:--:--")

    def _on_schedule_tick(self):
        if not self.schedule_active or self.schedule_time is None:
            return
        remaining = self._schedule_remaining_seconds()
        if remaining <= 0:
            self.stop_schedule()
            self._start_auto_analysis(silent=True)
        else:
            self._update_countdown(remaining)

    def _schedule_remaining_seconds(self):
        if self.schedule_time is None:
            return -1
        now = QTime.currentTime()
        secs = now.secsTo(self.schedule_time)
        if secs < 0:
            secs += 86400
        return secs

    def _update_countdown(self, remaining=None):
        if remaining is None:
            remaining = self._schedule_remaining_seconds()
        if remaining < 0:
            self.label_countdown.setText("下次执行倒计时：--:--:--")
            return
        h = remaining // 3600
        m = (remaining % 3600) // 60
        s = remaining % 60
        self.label_countdown.setText(f"下次执行倒计时：{h:02d}:{m:02d}:{s:02d}")

    def start_scan(self):
        path = self.edit_path.text().strip().strip("|").strip()
        self.edit_path.setText(path)
        key_raw = self.edit_keyword.text().strip()
        if not path:
            QMessageBox.warning(self, "错误", "请选择有效的文件夹或文件路径")
            return
        if not os.path.exists(path):
            QMessageBox.warning(self, "错误", "路径不存在")
            return
        if self.scan_thread is not None and self.scan_thread.isRunning():
            return

        keywords = [k.strip() for k in key_raw.split(",") if k.strip()]
        selected_levels = self.current_levels()

        ext = self.combo_ext.currentText()
        is_single = os.path.isfile(path)
        if is_single:
            folder = os.path.dirname(path)
        else:
            folder = path

        self.search_results.clear()
        self.tab_result.clear()
        self.progress_bar.setValue(0)
        self.label_stat.setText("扫描中...")
        self.find_index = 0
        self.last_find_text = ""
        self.btn_export_csv.setEnabled(False)
        self.btn_start_scan.setEnabled(False)
        self.btn_stop_scan.setEnabled(True)

        self.scan_thread = FolderScanThread(
            folder=folder, keywords=keywords, ext_filter=ext,
            selected_levels=selected_levels, extract_value=False
        )
        if is_single:
            self.scan_thread._collect_files = lambda: [path]

        self.scan_thread.sig_progress.connect(self._on_progress)
        self.scan_thread.sig_result_batch.connect(self._on_result_batch)
        self.scan_thread.sig_raw_text.connect(self._on_raw_text)
        self.scan_thread.sig_stat_update.connect(self._on_stat_update)
        self.scan_thread.sig_error_log.connect(lambda m: self.label_stat.setText(m))
        self.scan_thread.sig_finished.connect(self._on_scan_finished)
        self.scan_thread.start()

    def stop_scan(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.stop()
            self.scan_thread.wait()

    def _on_progress(self, p, cur, total):
        self.progress_bar.setValue(p)

    def _on_stat_update(self, total_hit, kw_dict):
        parts = [f"{k}:{v}" for k, v in kw_dict.items()]
        self.label_stat.setText(f"扫描中... 命中 {total_hit} | {', '.join(parts)}")

    def _flush_buf_to_result(self):
        if not self._ui_buf:
            return
        chunk = "\n".join(self._ui_buf)
        self.tab_result.setUpdatesEnabled(False)
        self.tab_result.appendPlainText(chunk)
        self.tab_result.setUpdatesEnabled(True)
        self.tab_result.viewport().update()
        self._ui_buf.clear()

    def _on_result_batch(self, batch: List[SearchResultItem]):
        for item in batch:
            short_name = os.path.basename(item.filepath)
            self._ui_buf.append(f"[{short_name}] L{item.line_no}: {item.line_content}")
        self.search_results.extend(batch)

        if len(self._ui_buf) >= 2000:
            self._flush_buf_to_result()

    def _on_raw_text(self, fp: str, text: str):
        self._flush_buf_to_result()
        short_name = os.path.basename(fp)
        self.tab_result.setUpdatesEnabled(False)
        self.tab_result.appendPlainText(f"===== {short_name} =====")
        self.tab_result.appendPlainText(text.rstrip("\n"))
        self.tab_result.setUpdatesEnabled(True)
        self.tab_result.viewport().update()

    def _on_scan_finished(self, hit_dict, kw_hit_dict, ref_data_list, csv_items):
        self._flush_buf_to_result()

        self.progress_bar.setValue(100)
        self.btn_start_scan.setEnabled(True)
        self.btn_stop_scan.setEnabled(False)

        self.csv_hit_items = csv_items

        total_hit = sum(hit_dict.values())
        parts = [f"关键字【{k}】命中 {v}" for k, v in kw_hit_dict.items()]
        stat_text = f"✅ 扫描完成 | 共扫 {len(hit_dict)} 个文件 | 总命中 {total_hit} 条 | " + " | ".join(parts)
        self.label_stat.setText(stat_text)

        if len(csv_items) > 0:
            self.btn_export_csv.setEnabled(True)
        else:
            self.btn_export_csv.setEnabled(False)

        if total_hit == 0:
            QMessageBox.information(self, "完成", "扫描完成，但没有命中任何行")
        else:
            QMessageBox.information(self, "完成",
                                    f"扫描完成！\n文件数：{len(hit_dict)}\n总命中：{total_hit} 条\n" + "\n".join(parts))

    def export_csv_result(self):
        if not self.csv_hit_items:
            QMessageBox.warning(self, "提示", "没有导出的命中记录")
            return
        if self._csv_exporting:
            QMessageBox.information(self, "提示", "正在导出中，请稍候")
            return

        save_path, _ = QFileDialog.getSaveFileName(
            self, "保存CSV", "scan_result.csv", "CSV Files (*.csv)"
        )
        if not save_path:
            return

        self._csv_exporting = True
        self._csv_export_cancel = False
        self.btn_export_csv.setEnabled(False)

        def on_done(ok):
            self._csv_exporting = False
            self.btn_export_csv.setEnabled(True)

        _export_csv_in_main_thread(self, save_path, self.csv_hit_items, on_done,
                                    cancel_flag_getter=lambda: self._csv_export_cancel)

    def find_next(self):
        if self.find_busy:
            return
        txt = self.edit_find.text().strip()
        if not txt or len(self.search_results) == 0:
            return
        self.find_busy = True

        if txt != self.last_find_text:
            self.find_index = 0
            self.last_find_text = txt

        found_pos = -1
        for i in range(self.find_index, len(self.search_results)):
            if txt in self.search_results[i].line_content:
                found_pos = i
                break
        if found_pos == -1:
            for i in range(0, self.find_index):
                if txt in self.search_results[i].line_content:
                    found_pos = i
                    break

        if found_pos >= 0:
            self.find_index = found_pos + 1
            item = self.search_results[found_pos]
            line_text = f"[{os.path.basename(item.filepath)}] L{item.line_no}: {item.line_content}"
            doc = self.tab_result.document()
            cursor = doc.find(line_text)
            if not cursor.isNull():
                self.tab_result.setTextCursor(cursor)
                self.tab_result.ensureCursorVisible()
        else:
            QMessageBox.information(self, "提示", "没有更匹配结果")

        QTimer.singleShot(200, self._release_find_busy)

    def find_prev(self):
        if self.find_busy:
            return
        txt = self.edit_find.text().strip()
        if not txt or len(self.search_results) == 0:
            return
        self.find_busy = True

        if txt != self.last_find_text:
            self.find_index = len(self.search_results) - 1
            self.last_find_text = txt

        found_pos = -1
        for i in range(self.find_index, -1, -1):
            if txt in self.search_results[i].line_content:
                found_pos = i
                break
        if found_pos == -1:
            for i in range(len(self.search_results) - 1, self.find_index, -1):
                if txt in self.search_results[i].line_content:
                    found_pos = i
                    break

        if found_pos >= 0:
            self.find_index = found_pos - 1
            item = self.search_results[found_pos]
            line_text = f"[{os.path.basename(item.filepath)}] L{item.line_no}: {item.line_content}"
            doc = self.tab_result.document()
            cursor = doc.find(line_text)
            if not cursor.isNull():
                self.tab_result.setTextCursor(cursor)
                self.tab_result.ensureCursorVisible()
        else:
            QMessageBox.information(self, "提示", "没有更匹配结果")

        QTimer.singleShot(200, self._release_find_busy)

    def _release_find_busy(self):
        self.find_busy = False

    def _clean_path(self, p):
        return p.strip().strip("|").strip()

    def select_filter_out(self):
        p = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if p:
            self.edit_filter_out.setText(p)

    def run_filter_export(self):
        src = self._clean_path(self.edit_path.text())
        out_dir = self._clean_path(self.edit_filter_out.text())
        fkey = self.edit_filter_key.text().strip()
        if not src:
            QMessageBox.warning(self, "缺少源路径", "主页面扫描路径为空！请先在主页面选择文件或文件夹")
            return
        if not os.path.exists(src):
            QMessageBox.warning(self, "路径无效", f"路径不存在：{src}")
            return
        if not out_dir:
            QMessageBox.warning(self, "缺少输出目录", "请选择输出目录")
            return
        if not fkey:
            QMessageBox.warning(self, "缺少关键字", "请填写过滤关键字（逗号分隔）")
            return
        if self.filter_thread and self.filter_thread.isRunning():
            QMessageBox.information(self, "提示", "导出任务正在运行，等待完成")
            return

        is_dir = os.path.isdir(src)
        self.filter_log.clear()
        self.filter_thread = FilterExportThread(src, out_dir, fkey, is_dir)
        self.filter_thread.sig_log.connect(self.filter_log.appendPlainText)
        self.filter_thread.sig_finished.connect(self.on_filter_finished)

        self.btn_run_filter.setEnabled(False)
        self.btn_stop_filter.setEnabled(True)
        self.progress_filter.setVisible(True)
        self.filter_log.appendPlainText(f"===== 过滤导出开始 =====")
        self.filter_log.appendPlainText(f"  源:     {src}")
        self.filter_log.appendPlainText(f"  输出:   {out_dir}")
        self.filter_log.appendPlainText(f"  模式:   {'目录批量' if is_dir else '单文件'}")
        self.filter_log.appendPlainText(f"  关键字: {fkey}")
        self.filter_thread.start()

    def stop_filter_export(self):
        if self.filter_thread and self.filter_thread.isRunning():
            self.filter_thread.stop()
            self.filter_log.appendPlainText("已请求停止，等待当前文件处理完毕...")

    def on_filter_finished(self, ok, total_lines, file_cnt):
        self.btn_run_filter.setEnabled(True)
        self.btn_stop_filter.setEnabled(False)
        self.progress_filter.setVisible(False)
        if ok:
            self.filter_log.appendPlainText(f"✅ 完成 | {file_cnt} 个文件 | {total_lines} 行")
        else:
            self.filter_log.appendPlainText("❌ 失败或已取消")
        self.filter_thread = None

    def closeEvent(self, event):
        self.stop_scan()
        self._csv_export_cancel = True
        self.schedule_timer.stop()
        if self.filter_thread and self.filter_thread.isRunning():
            self.filter_thread.stop()
            self.filter_thread.wait(3000)
        if self.auto_thread is not None and self.auto_thread.isRunning():
            self.auto_thread.stop()
            self.auto_thread.wait(3000)
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
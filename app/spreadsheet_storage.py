"""
Módulo de Armazenamento e Backup de Planilhas para o DateLimit.

Garante que toda planilha enviada (seja com sucesso, erro ou necessidade de mapeamento)
tenha uma cópia de segurança salva em disco no servidor, permitindo:
1. Download do arquivo original a qualquer momento;
2. Re-importação instantânea sem precisar reenviar o arquivo;
3. Histórico e auditoria dos arquivos recebidos com retenção automática.
"""

import os
import json
import re
from pathlib import Path
from datetime import datetime
from uuid import uuid4

BASE_DIR = Path(__file__).resolve().parent.parent
BACKUP_DIR = BASE_DIR / "database" / "spreadsheet_backups"
INDEX_FILE = BACKUP_DIR / "index.json"
MAX_SAVED_SPREADSHEETS = 30


def _ensure_dir():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_FILE.exists():
        try:
            with open(INDEX_FILE, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _load_index() -> list[dict]:
    _ensure_dir()
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_index(entries: list[dict]):
    _ensure_dir()
    try:
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Erro ao salvar índice de planilhas:", e)


def sanitize_filename(filename: str) -> str:
    """Remove caracteres especiais perigosos mantendo o nome legível e a extensão."""
    clean = re.sub(r'[^\w.\-]', '_', filename)
    clean = re.sub(r'_+', '_', clean)
    return clean or "planilha.xls"


def format_bytes(size: int) -> str:
    """Formata tamanho de arquivo em KB ou MB legível."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.2f} MB"


def save_spreadsheet_backup(
    file_bytes: bytes,
    original_filename: str,
    status: str = "RECEBIDO",
    details: str = "",
    total_rows: int = 0
) -> dict:
    """
    Salva uma cópia física da planilha no diretório de backups e registra seus metadados.
    Executa rotação automática mantendo até MAX_SAVED_SPREADSHEETS arquivos.
    """
    _ensure_dir()

    now = datetime.now()
    file_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
    clean_name = sanitize_filename(original_filename)
    stored_filename = f"{file_id}_{clean_name}"
    file_path = BACKUP_DIR / stored_filename

    # Grava arquivo binário em disco
    with open(file_path, "wb") as f:
        f.write(file_bytes)

    size_bytes = len(file_bytes)
    entry = {
        "id": file_id,
        "original_filename": original_filename,
        "stored_filename": stored_filename,
        "size_bytes": size_bytes,
        "size_display": format_bytes(size_bytes),
        "timestamp": now.isoformat(),
        "timestamp_display": now.strftime("%d/%m/%Y %H:%M:%S"),
        "status": status,
        "details": details,
        "total_rows": total_rows
    }

    # Atualiza índice
    entries = _load_index()
    entries.insert(0, entry)

    # Rotação automática para liberar espaço em disco
    if len(entries) > MAX_SAVED_SPREADSHEETS:
        to_prune = entries[MAX_SAVED_SPREADSHEETS:]
        entries = entries[:MAX_SAVED_SPREADSHEETS]
        for old in to_prune:
            old_path = BACKUP_DIR / old.get("stored_filename", "")
            if old_path.exists():
                try:
                    old_path.unlink()
                except Exception:
                    pass

    _save_index(entries)
    return entry


def update_spreadsheet_backup_status(
    file_id: str,
    status: str,
    details: str = None,
    total_rows: int = None
) -> dict | None:
    """Atualiza o status e os detalhes de auditoria de uma planilha salva."""
    entries = _load_index()
    target = None
    for item in entries:
        if item.get("id") == file_id:
            target = item
            item["status"] = status
            if details is not None:
                item["details"] = details
            if total_rows is not None:
                item["total_rows"] = total_rows
            break

    if target:
        _save_index(entries)
    return target


def get_spreadsheet_backups() -> list[dict]:
    """Retorna lista de todas as planilhas salvas, mais recentes primeiro."""
    return _load_index()


def get_spreadsheet_backup_by_id(file_id: str) -> tuple[bytes, str, dict] | None:
    """
    Localiza a planilha salva pelo ID e retorna:
    (conteúdo em bytes, nome original do arquivo, metadados)
    """
    entries = _load_index()
    for item in entries:
        if item.get("id") == file_id:
            file_path = BACKUP_DIR / item.get("stored_filename", "")
            if file_path.exists():
                with open(file_path, "rb") as f:
                    content = f.read()
                return content, item.get("original_filename", "planilha.xls"), item
    return None


def delete_spreadsheet_backup(file_id: str) -> bool:
    """Remove uma planilha salva do disco e do índice."""
    entries = _load_index()
    new_entries = []
    removed = False
    for item in entries:
        if item.get("id") == file_id:
            removed = True
            file_path = BACKUP_DIR / item.get("stored_filename", "")
            if file_path.exists():
                try:
                    file_path.unlink()
                except Exception:
                    pass
        else:
            new_entries.append(item)

    if removed:
        _save_index(new_entries)
    return removed

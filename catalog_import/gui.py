from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Qt, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont, QIcon, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .app_update import (
    AppUpdateError,
    apply_update_and_relaunch,
    check_and_download_update,
    current_install_path,
    is_frozen_app,
    resolve_update_target_path,
)
from .auth import AuthError, login_pfs
from .category_mapping import (
    CategoryMappingEntry,
    CategoryMappingStore,
    category_options_from_reference,
    children_of,
    pfs_category_label,
)
from .config import (
    APP_NAME,
    APP_VERSION,
    ICON_ICNS,
    ICON_PNG,
    UPDATE_CHECK_INTERVAL_MS,
)
from .mel_service import MelSyncError, send_products_to_efashion
from .efashion_auth import EfashionAuthError, login_efashion
from .efashion_client import EfashionApiError, EfashionClient
from .pfs_client import (
    PfsApiError,
    PRODUCT_TABLE_HEADERS,
    format_product_details,
    product_images_by_color,
    product_table_row,
)
from .product_service import fetch_pfs_products
from .session_store import AppSession, SessionStore
from .update_service import update_products_on_efashion


THUMB_SIZE = 132


class ClickableLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class CategoryMappingDialog(QDialog):
    """Popup L1 → L2 → L3 pour une catégorie source inconnue."""

    def __init__(
        self,
        *,
        mapping_key: str,
        pfs_label: str,
        gender: str,
        l1_options: list[dict],
        l2_options: list[dict],
        l3_options: list[dict],
        initial_entry: CategoryMappingEntry | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Mapping catégorie EFashion")
        self.setMinimumWidth(460)
        self.mapping_key = mapping_key
        self.pfs_label = pfs_label
        self.gender = gender
        self._l1 = l1_options
        self._l2 = l2_options
        self._l3 = l3_options
        self.selected_entry: CategoryMappingEntry | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        info = QLabel(
            f"Catégorie source « {pfs_label} »"
            + (f" ({gender})" if gender and gender != "*" else "")
            + "\nn’a pas encore de feuille EFashion associée.\n"
            "Choisissez la catégorie L1 → L2 → L3."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.l1_combo = QComboBox()
        self.l2_combo = QComboBox()
        self.l3_combo = QComboBox()
        for combo, title in (
            (self.l1_combo, "Catégorie (L1)"),
            (self.l2_combo, "Sous-catégorie (L2)"),
            (self.l3_combo, "Feuille (L3)"),
        ):
            layout.addWidget(QLabel(title))
            layout.addWidget(combo)

        self.l1_combo.addItem("— Choisir —", "")
        for item in sorted(self._l1, key=lambda x: str(x.get("label") or "")):
            self.l1_combo.addItem(str(item.get("label") or item["id"]), str(item["id"]))

        self.l1_combo.currentIndexChanged.connect(self._on_l1_changed)
        self.l2_combo.currentIndexChanged.connect(self._on_l2_changed)
        self._on_l1_changed()

        if initial_entry and initial_entry.l1_id:
            self._select_combo_data(self.l1_combo, initial_entry.l1_id)
            self._on_l1_changed()
            if initial_entry.l2_id:
                self._select_combo_data(self.l2_combo, initial_entry.l2_id)
                self._on_l2_changed()
                if initial_entry.id:
                    self._select_combo_data(self.l3_combo, initial_entry.id)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _select_combo_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(str(value))
        if index >= 0:
            combo.setCurrentIndex(index)

    def _on_l1_changed(self) -> None:
        self.l2_combo.blockSignals(True)
        self.l2_combo.clear()
        self.l2_combo.addItem("— Choisir —", "")
        parent_id = str(self.l1_combo.currentData() or "")
        if parent_id:
            for item in sorted(
                children_of(self._l2, parent_id),
                key=lambda x: str(x.get("label") or ""),
            ):
                self.l2_combo.addItem(
                    str(item.get("label") or item["id"]), str(item["id"])
                )
        self.l2_combo.blockSignals(False)
        self._on_l2_changed()

    def _on_l2_changed(self) -> None:
        self.l3_combo.clear()
        self.l3_combo.addItem("— Choisir —", "")
        parent_id = str(self.l2_combo.currentData() or "")
        if parent_id:
            for item in sorted(
                children_of(self._l3, parent_id),
                key=lambda x: str(x.get("label") or ""),
            ):
                self.l3_combo.addItem(
                    str(item.get("label") or item["id"]), str(item["id"])
                )

    def _on_accept(self) -> None:
        l1_id = str(self.l1_combo.currentData() or "")
        l2_id = str(self.l2_combo.currentData() or "")
        l3_id = str(self.l3_combo.currentData() or "")
        if not l1_id or not l2_id or not l3_id:
            QMessageBox.warning(
                self,
                APP_NAME,
                "Sélectionnez une catégorie complète (L1, L2 et L3).",
            )
            return
        label = " > ".join(
            [
                self.l1_combo.currentText(),
                self.l2_combo.currentText(),
                self.l3_combo.currentText(),
            ]
        )
        self.selected_entry = CategoryMappingEntry(
            id=l3_id,
            label=label,
            l1_id=l1_id,
            l2_id=l2_id,
            pfs_category=self.pfs_label,
            gender=self.gender,
        )
        self.accept()


def _gender_display_label(gender: str) -> str:
    labels = {
        "MAN": "Homme",
        "WOMAN": "Femme",
        "UNISEX": "Unisex",
        "KID": "Enfant",
        "*": "Tous genres",
    }
    return labels.get(str(gender or "").strip().upper(), gender or "—")


def _split_mapping_key(key: str) -> tuple[str, str]:
    if "|" in key:
        category, gender = key.split("|", 1)
        return category, gender
    return key, "*"


class CategoryMappingManagerDialog(QDialog):
    """Liste visuelle des correspondances catégories source → EFashion."""

    def __init__(
        self,
        *,
        store: CategoryMappingStore,
        id_vendeur: int,
        efashion_session,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Mapping catégories")
        self.setMinimumSize(780, 420)
        self.store = store
        self.id_vendeur = id_vendeur
        self.efashion_session = efashion_session
        self._reference_data: dict | None = None
        self.changed = False

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        intro = QLabel(
            "Correspondances enregistrées entre vos catégories source et le catalogue EFashion. "
            "Sélectionnez une ligne pour la modifier ou la supprimer."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #374151;")
        layout.addWidget(intro)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            ["Catégorie source", "Genre", "Catégorie EFashion"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self.table)

        self.empty_label = QLabel("Aucun mapping enregistré pour l'instant.")
        self.empty_label.setStyleSheet("color: #6b7280; font-style: italic;")
        self.empty_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.empty_label)

        buttons = QHBoxLayout()
        self.edit_button = QPushButton("Modifier")
        self.edit_button.clicked.connect(self._on_edit)
        buttons.addWidget(self.edit_button)

        self.delete_button = QPushButton("Supprimer")
        self.delete_button.setProperty("class", "secondary")
        self.delete_button.clicked.connect(self._on_delete)
        buttons.addWidget(self.delete_button)

        buttons.addStretch()

        close_button = QPushButton("Fermer")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._reload_table()

    def _reload_table(self) -> None:
        entries = self.store.sorted_entries()
        self.table.setRowCount(len(entries))
        has_rows = bool(entries)
        self.table.setVisible(has_rows)
        self.empty_label.setVisible(not has_rows)
        self.edit_button.setEnabled(has_rows)
        self.delete_button.setEnabled(has_rows)

        for row, (key, entry) in enumerate(entries):
            category, gender = _split_mapping_key(key)
            pfs_label = entry.pfs_category or category
            self.table.setItem(row, 0, QTableWidgetItem(pfs_label))
            self.table.setItem(row, 1, QTableWidgetItem(_gender_display_label(gender)))
            ef_label = entry.label or entry.id
            ef_item = QTableWidgetItem(ef_label)
            ef_item.setData(Qt.UserRole, key)
            self.table.setItem(row, 2, ef_item)

        if has_rows:
            self.table.selectRow(0)

    def _selected_key(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 2)
        if item is None:
            return None
        key = item.data(Qt.UserRole)
        return str(key) if key else None

    def _load_reference_data(self) -> dict | None:
        if self._reference_data is not None:
            return self._reference_data
        try:
            with EfashionClient(self.efashion_session) as client:
                self._reference_data = client.get_reference_data()
        except EfashionApiError:
            return None
        return self._reference_data

    def _on_edit(self) -> None:
        key = self._selected_key()
        if not key:
            QMessageBox.information(self, APP_NAME, "Sélectionnez un mapping à modifier.")
            return

        entry = self.store.get(key)
        if entry is None:
            self._reload_table()
            return

        reference_data = self._load_reference_data()
        if not reference_data:
            QMessageBox.critical(
                self,
                APP_NAME,
                "Impossible de charger les catégories EFashion.",
            )
            return

        category, gender = _split_mapping_key(key)
        pfs_label = entry.pfs_category or category or "Sans catégorie"
        l1, l2, l3 = category_options_from_reference(reference_data)
        dialog = CategoryMappingDialog(
            mapping_key=key,
            pfs_label=pfs_label,
            gender=gender,
            l1_options=l1,
            l2_options=l2,
            l3_options=l3,
            initial_entry=entry,
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted or not dialog.selected_entry:
            return

        self.store.set_entry(key, dialog.selected_entry)
        self.changed = True
        self._reload_table()

    def _on_delete(self) -> None:
        key = self._selected_key()
        if not key:
            QMessageBox.information(self, APP_NAME, "Sélectionnez un mapping à supprimer.")
            return

        entry = self.store.get(key)
        category, gender = _split_mapping_key(key)
        pfs_label = (entry.pfs_category if entry else None) or category
        ef_label = entry.label if entry else "—"

        reply = QMessageBox.question(
            self,
            "Supprimer le mapping",
            (
                f"Supprimer la correspondance ?\n\n"
                f"Source : {pfs_label} ({_gender_display_label(gender)})\n"
                f"EFashion : {ef_label}\n\n"
                "Ce mapping vous sera redemandé au prochain envoi."
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.store.delete_entry(key)
        self.changed = True
        self._reload_table()


class AppUpdateWorker(QObject):
    """Check GitHub releases en arrière-plan (n'utilise pas le worker principal)."""

    finished = Signal()
    available = Signal(str, str, str)  # version, file_path, html_url
    failed = Signal(str)

    def run(self) -> None:
        try:
            result = check_and_download_update()
            if result is not None:
                release, path = result
                self.available.emit(release.version, str(path), release.html_url)
        except AppUpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class Worker(QObject):
    finished = Signal()
    failed = Signal(str)
    log = Signal(str)
    progress = Signal(int, int, str)
    pfs_login_done = Signal()
    efashion_step_done = Signal()
    products_ready = Signal(object, object, object)
    existing_refs_ready = Signal(object)
    send_done = Signal(int, str, object)
    update_done = Signal(int, str)

    def __init__(
        self,
        task: str,
        app_session: AppSession | None = None,
        store: SessionStore | None = None,
        email: str = "",
        password: str = "",
        output_dir: str = "",
        products: list | None = None,
        raw_pages: list | None = None,
        raw_variant_pages: list | None = None,
    ) -> None:
        super().__init__()
        self.task = task
        self.app_session = app_session
        self.store = store
        self.email = email
        self.password = password
        self.output_dir = output_dir
        self.products = products or []
        self.raw_pages = raw_pages or []
        self.raw_variant_pages = raw_variant_pages or []

    def run(self) -> None:
        try:
            if self.task == "pfs_login":
                assert self.store is not None
                pfs = login_pfs(
                    self.email,
                    self.password,
                    self.store,
                    self.app_session,
                )
                self.log.emit(f"Compte source connecté : {pfs.email}")
                self.pfs_login_done.emit()

            elif self.task == "efashion_login":
                assert self.store is not None and self.app_session is not None
                efashion = login_efashion(
                    self.email,
                    self.password,
                    self.store,
                    self.app_session,
                )
                self.log.emit(f"EFashion connecté : {efashion.email}")
                self.efashion_step_done.emit()

            elif self.task == "fetch_products":
                assert self.app_session and self.app_session.pfs

                def on_progress(done: int, total: int, message: str) -> None:
                    self.progress.emit(done, total, message)

                products, raw_pages, raw_variant_pages = fetch_pfs_products(
                    self.app_session.pfs,
                    on_progress=on_progress,
                )
                if not products:
                    raise PfsApiError(
                        "Aucun produit trouvé sur votre compte source."
                    )
                self.products_ready.emit(products, raw_pages, raw_variant_pages)

            elif self.task == "check_existing_refs":
                assert self.app_session and self.app_session.efashion
                refs_payload = []
                seen: set[str] = set()
                for product in self.products:
                    reference = str(product.get("reference") or "").strip()
                    if not reference or reference in seen:
                        continue
                    seen.add(reference)
                    refs_payload.append({"reference": reference})

                total_refs = max(len(refs_payload), 1)

                def on_dup_progress(checked: int, total: int) -> None:
                    self.progress.emit(
                        checked,
                        max(total, 1),
                        f"Vérification des doublons : {checked}/{total} référence(s)…",
                    )

                with EfashionClient(self.app_session.efashion) as client:
                    results = client.check_references_exists_batch(
                        refs_payload,
                        on_progress=on_dup_progress,
                    )
                existing = {
                    reference for reference, exists in results.items() if exists
                }
                self.progress.emit(
                    total_refs,
                    total_refs,
                    f"Vérification terminée — {len(existing)} référence(s) déjà présentes.",
                )
                self.existing_refs_ready.emit(existing)

            elif self.task == "send_to_efashion":
                assert self.app_session and self.app_session.efashion
                assert self.app_session.pfs

                def on_progress(done: int, total: int, message: str) -> None:
                    self.progress.emit(done, total, message)

                result = send_products_to_efashion(
                    self.app_session.efashion,
                    self.products,
                    pfs_session=self.app_session.pfs,
                    on_progress=on_progress,
                )
                self.send_done.emit(
                    int(result["total"]),
                    str(result.get("message") or ""),
                    result.get("references") or [],
                )

            elif self.task == "update_on_efashion":
                assert self.app_session and self.app_session.efashion
                assert self.app_session.pfs

                def on_progress(done: int, total: int, message: str) -> None:
                    self.progress.emit(done, total, message)

                result = update_products_on_efashion(
                    self.app_session.efashion,
                    self.products,
                    pfs_session=self.app_session.pfs,
                    on_progress=on_progress,
                )
                self.update_done.emit(
                    int(result["total"]),
                    str(result.get("message") or ""),
                )

        except (AuthError, EfashionAuthError, MelSyncError, PfsApiError, Exception) as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class CatalogDesktopApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        icon = _app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self.resize(980, 720)

        self.session_store = SessionStore()
        self.app_session: AppSession | None = self.session_store.load()
        self.products: list[dict] = []
        self.displayed_create_products: list[dict] = []
        self.displayed_existing_products: list[dict] = []
        self.raw_pages: list[dict] = []
        self.raw_variant_pages: list[dict] = []
        self.selected_ids: set[str] = set()
        self.selected_update_ids: set[str] = set()
        self.existing_references: set[str] = set()
        self._pending_existing_check = False
        self._existing_refs_verified = False
        self._busy = False
        self._thread: QThread | None = None
        self._worker: Worker | None = None
        self._pending_task = ""
        self._updating_table = False

        self._net_manager = QNetworkAccessManager(self)
        self._image_token = 0
        self._pending_replies: dict[QNetworkReply, tuple[int, ClickableLabel]] = {}
        self._update_thread: QThread | None = None
        self._update_worker: AppUpdateWorker | None = None
        self._update_prompted_version: str | None = None
        self._update_error_shown = False

        self._build_ui()
        self._apply_global_styles()
        self._show_screen()
        self._start_update_scheduler()

    def _apply_global_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #f3f4f6;
                color: #111827;
            }
            QLineEdit, QTextEdit {
                color: #111827;
                background: #ffffff;
                border: 1px solid #d1d5db;
                border-radius: 8px;
                padding: 10px;
                selection-background-color: #bfdbfe;
                selection-color: #111827;
            }
            QLineEdit:disabled, QTextEdit:disabled {
                color: #6b7280;
                background: #f9fafb;
            }
            QTableWidget {
                color: #111827;
                background: #ffffff;
                gridline-color: #e5e7eb;
            }
            QHeaderView::section {
                color: #111827;
                background: #f9fafb;
            }
            """
        )

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 24, 24, 24)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        self.pfs_login_page = self._build_pfs_login_page()
        self.efashion_login_page = self._build_efashion_login_page()
        self.main_page = self._build_main_page()

        self.stack.addWidget(self.pfs_login_page)
        self.stack.addWidget(self.efashion_login_page)
        self.stack.addWidget(self.main_page)

    def _card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card.setStyleSheet(
            """
            QFrame#card {
                background: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 16px;
            }
            QLabel.title {
                font-size: 26px;
                font-weight: 700;
                color: #111827;
            }
            QLabel.subtitle {
                color: #6b7280;
            }
            QLabel.step {
                color: #2563eb;
                font-weight: 600;
            }
            QLineEdit, QTextEdit {
                border: 1px solid #d1d5db;
                border-radius: 8px;
                padding: 10px;
                background: #ffffff;
                color: #111827;
            }
            QPushButton[class="primary"] {
                background: #2563eb;
                color: white;
                border: 1px solid #1e3a8a;
                border-radius: 8px;
                padding: 10px 18px;
                font-weight: 700;
                font-size: 14px;
                min-height: 36px;
            }
            QPushButton[class="primary"]:hover {
                background: #1d4ed8;
            }
            QPushButton[class="primary"]:disabled {
                background: #d1d5db;
                color: #f9fafb;
                border: 1px solid #9ca3af;
            }
            QPushButton[class="secondary"] {
                background: #f3f4f6;
                color: #111827;
                border: 1px solid #d1d5db;
                border-radius: 8px;
                padding: 8px 14px;
                font-weight: 600;
                font-size: 13px;
            }
            QPushButton[class="secondary"]:hover {
                background: #e5e7eb;
            }
            QPushButton[class="secondary"]:disabled {
                color: #9ca3af;
                background: #f3f4f6;
            }
            QPushButton[class="accent"] {
                background: #0f766e;
                color: white;
                border: 1px solid #115e59;
                border-radius: 8px;
                padding: 10px 18px;
                font-weight: 700;
                font-size: 14px;
                min-height: 36px;
            }
            QPushButton[class="accent"]:hover {
                background: #0d9488;
            }
            QPushButton[class="accent"]:disabled {
                background: #d1d5db;
                color: #f9fafb;
                border: 1px solid #9ca3af;
            }
            QTableWidget {
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                background: #ffffff;
            }
            """
        )
        return card

    def _build_pfs_login_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addStretch()

        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(36, 36, 36, 36)
        layout.setSpacing(12)

        step = QLabel("Étape 1 / 2")
        step.setProperty("class", "step")
        step.setAlignment(Qt.AlignCenter)
        layout.addWidget(step)

        title = QLabel("Connexion compte source")
        title.setProperty("class", "title")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel(
            "Connectez-vous avec les identifiants de votre compte source."
        )
        subtitle.setProperty("class", "subtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.pfs_email_input = QLineEdit()
        self.pfs_email_input.setPlaceholderText("Email compte source")
        layout.addWidget(self.pfs_email_input)

        self.pfs_password_input = QLineEdit()
        self.pfs_password_input.setPlaceholderText("Mot de passe compte source")
        self.pfs_password_input.setEchoMode(QLineEdit.Password)
        layout.addWidget(self.pfs_password_input)

        self.pfs_login_button = QPushButton("Se connecter au compte source")
        self.pfs_login_button.setProperty("class", "primary")
        self.pfs_login_button.clicked.connect(self._on_pfs_login)
        layout.addWidget(self.pfs_login_button)

        self.pfs_login_status = QLabel("")
        self.pfs_login_status.setAlignment(Qt.AlignCenter)
        self.pfs_login_status.setStyleSheet("color: #6b7280;")
        layout.addWidget(self.pfs_login_status)

        if self.app_session and self.app_session.pfs:
            self.pfs_email_input.setText(self.app_session.pfs.email)
            self.pfs_login_status.setText(
                f"Compte source : {self.app_session.pfs.email}"
            )
            self.pfs_login_status.setStyleSheet("color: #15803d;")

        outer.addWidget(card, alignment=Qt.AlignCenter)
        outer.addStretch()
        return page

    def _build_efashion_login_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addStretch()

        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(36, 36, 36, 36)
        layout.setSpacing(12)

        step = QLabel("Étape 2 / 2")
        step.setProperty("class", "step")
        step.setAlignment(Qt.AlignCenter)
        layout.addWidget(step)

        title = QLabel("Connexion vendeur EFashion")
        title.setProperty("class", "title")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel(
            "Connectez-vous avec votre compte vendeur EFashion Paris."
        )
        subtitle.setProperty("class", "subtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        self.efashion_email_input = QLineEdit()
        self.efashion_email_input.setPlaceholderText("Email EFashion")
        layout.addWidget(self.efashion_email_input)

        self.efashion_password_input = QLineEdit()
        self.efashion_password_input.setPlaceholderText("Mot de passe EFashion")
        self.efashion_password_input.setEchoMode(QLineEdit.Password)
        layout.addWidget(self.efashion_password_input)

        self.efashion_login_button = QPushButton("Se connecter à EFashion")
        self.efashion_login_button.setProperty("class", "primary")
        self.efashion_login_button.clicked.connect(self._on_efashion_login)
        layout.addWidget(self.efashion_login_button)

        self.efashion_login_status = QLabel("")
        self.efashion_login_status.setAlignment(Qt.AlignCenter)
        self.efashion_login_status.setStyleSheet("color: #6b7280;")
        layout.addWidget(self.efashion_login_status)

        outer.addWidget(card, alignment=Qt.AlignCenter)
        outer.addStretch()
        return page

    def _build_main_page(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(14)
        layout.setContentsMargins(0, 0, 12, 0)

        header = QHBoxLayout()
        title = QLabel(APP_NAME)
        title.setFont(QFont("", 22, QFont.Bold))
        header.addWidget(title)
        header.addStretch()
        self.category_mapping_button = QPushButton("Mapping catégories")
        self.category_mapping_button.setProperty("class", "secondary")
        self.category_mapping_button.clicked.connect(self._on_manage_category_mappings)
        header.addWidget(self.category_mapping_button)
        self.logout_button = QPushButton("Déconnexion")
        self.logout_button.setProperty("class", "secondary")
        self.logout_button.clicked.connect(self._on_logout)
        header.addWidget(self.logout_button)
        layout.addLayout(header)

        status_card = self._card()
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(20, 16, 20, 16)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #374151;")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label)
        layout.addWidget(status_card)

        self.summary_label = QLabel("Chargement des produits en cours…")
        self.summary_label.setStyleSheet("color: #6b7280;")
        layout.addWidget(self.summary_label)

        self.filter_status_label = QLabel("")
        self.filter_status_label.setStyleSheet("color: #6b7280;")
        layout.addWidget(self.filter_status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.table_legend = QLabel(
            '<span style="color:#6b7280;">'
            "Tableau du haut = produits <b>à créer</b> sur le catalogue. "
            "Tableau du bas = produits <b>déjà en ligne</b> (mise à jour). "
            "Double-cliquez une <b>référence</b> du haut pour la renommer "
            "si besoin (ex. REF-PACK).</span>"
        )
        self.table_legend.setWordWrap(True)
        self.table_legend.setTextFormat(Qt.RichText)
        layout.addWidget(self.table_legend)

        # Filtre puis boutons d'action juste au-dessus des tableaux
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_label = QLabel("Filtrer")
        filter_label.setStyleSheet("color: #374151; font-weight: 600;")
        filter_row.addWidget(filter_label)

        self.reference_filter_input = QLineEdit()
        self.reference_filter_input.setPlaceholderText("référence (contient)")
        self.reference_filter_input.textChanged.connect(self._on_reference_filter_changed)
        self.reference_filter_input.setClearButtonEnabled(True)
        self.reference_filter_input.setMaximumWidth(280)
        filter_row.addWidget(self.reference_filter_input)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(10)

        self.fetch_button = QPushButton("Actualiser")
        self.fetch_button.setProperty("class", "accent")
        self.fetch_button.setStyleSheet(
            "QPushButton {"
            " background: #0f766e; color: white; border: 1px solid #115e59;"
            " border-radius: 8px; padding: 10px 18px; font-weight: 700; font-size: 14px;"
            " min-height: 36px;"
            "}"
            "QPushButton:hover { background: #0d9488; }"
            "QPushButton:disabled { background: #d1d5db; color: #f9fafb; border: 1px solid #9ca3af; }"
        )
        self.fetch_button.clicked.connect(self._on_fetch_products)
        buttons_row.addWidget(self.fetch_button)

        self.send_button = QPushButton("Créer les produits")
        self.send_button.setProperty("class", "primary")
        self.send_button.setStyleSheet(
            "QPushButton {"
            " background: #2563eb; color: white; border: 1px solid #1e3a8a;"
            " border-radius: 8px; padding: 10px 18px; font-weight: 700; font-size: 14px;"
            " min-height: 36px;"
            "}"
            "QPushButton:hover { background: #1d4ed8; }"
            "QPushButton:disabled { background: #d1d5db; color: #f9fafb; border: 1px solid #9ca3af; }"
        )
        self.send_button.setEnabled(False)
        self.send_button.clicked.connect(self._on_send_to_efashion)
        buttons_row.addWidget(self.send_button)

        self.update_button = QPushButton("Mettre à jour")
        self.update_button.setStyleSheet(
            "QPushButton {"
            " background: #b45309; color: white; border: 1px solid #92400e;"
            " border-radius: 8px; padding: 10px 18px; font-weight: 700; font-size: 14px;"
            " min-height: 36px;"
            "}"
            "QPushButton:hover { background: #d97706; }"
            "QPushButton:disabled { background: #d1d5db; color: #f9fafb; border: 1px solid #9ca3af; }"
        )
        self.update_button.setEnabled(False)
        self.update_button.clicked.connect(self._on_update_on_efashion)
        buttons_row.addWidget(self.update_button)

        buttons_row.addStretch()
        layout.addLayout(buttons_row)

        self._table_headers = ["", *PRODUCT_TABLE_HEADERS]

        create_label = QLabel("À créer sur le catalogue")
        create_label.setStyleSheet("color: #111827; font-weight: 700;")
        layout.addWidget(create_label)

        self.products_table = self._make_products_table(
            select_all_attr="select_all_checkbox",
            on_select_all=self._on_select_all_create_clicked,
            on_changed=self._on_create_item_changed,
            on_selected=self._on_create_product_selected,
        )
        layout.addWidget(self.products_table)

        existing_label = QLabel("Déjà en ligne — mise à jour")
        existing_label.setStyleSheet("color: #b45309; font-weight: 700;")
        layout.addWidget(existing_label)

        self.existing_table = self._make_products_table(
            select_all_attr="select_all_update_checkbox",
            on_select_all=self._on_select_all_update_clicked,
            on_changed=self._on_update_item_changed,
            on_selected=self._on_existing_product_selected,
            min_height=160,
        )
        layout.addWidget(self.existing_table)

        detail_label = QLabel("Détail du produit sélectionné")
        detail_label.setStyleSheet("color: #374151; font-weight: 600;")
        layout.addWidget(detail_label)

        self.product_detail_box = QTextEdit()
        self.product_detail_box.setReadOnly(True)
        self.product_detail_box.setPlaceholderText(
            "Cliquez sur une ligne pour voir compositions, variantes, stock et images."
        )
        self.product_detail_box.setMinimumHeight(160)
        layout.addWidget(self.product_detail_box)

        photos_label = QLabel("Photos par couleur (cliquez pour agrandir)")
        photos_label.setStyleSheet("color: #374151; font-weight: 600;")
        layout.addWidget(photos_label)

        self.image_scroll = QScrollArea()
        self.image_scroll.setWidgetResizable(True)
        self.image_scroll.setFixedHeight(THUMB_SIZE + 48)
        self.image_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.image_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.image_container = QWidget()
        self.image_layout = QHBoxLayout(self.image_container)
        self.image_layout.setContentsMargins(4, 4, 4, 4)
        self.image_layout.setSpacing(8)
        self.image_layout.addStretch()
        self.image_scroll.setWidget(self.image_container)
        layout.addWidget(self.image_scroll)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(content)
        page_layout.addWidget(scroll)

        return page

    def _is_setup_complete(self) -> bool:
        if not self.app_session or not self.app_session.pfs:
            return False
        return (
            self.app_session.efashion is not None
            and bool(self.app_session.efashion.access_token)
        )

    def _show_screen(self) -> None:
        self.app_session = self.session_store.load()

        if not self.app_session or not self.app_session.pfs:
            self.stack.setCurrentWidget(self.pfs_login_page)
            return

        if not self._is_setup_complete():
            if self.app_session.efashion:
                self.efashion_email_input.setText(self.app_session.efashion.email)
            self.stack.setCurrentWidget(self.efashion_login_page)
            return

        self.stack.setCurrentWidget(self.main_page)
        self._update_status_label()
        self._append_log(f"Compte source actif : {self.app_session.pfs.email}")
        self._auto_fetch_products_if_needed()

    def _auto_fetch_products_if_needed(self) -> None:
        if self.products:
            return
        if self._thread and self._thread.isRunning():
            return
        if not self.app_session or not self.app_session.pfs:
            return

        self.summary_label.setText("Récupération de vos produits…")
        self.progress_bar.setValue(0)
        self._append_log(
            "Chargement rapide : listProducts + listVariants "
            "(détail enrichi à l'envoi EFashion)…"
        )
        self._on_fetch_products()

    def _update_status_label(self) -> None:
        if not self.app_session or not self.app_session.pfs:
            self.status_label.setText("Non connecté.")
            return

        lines = [f"Source : {self.app_session.pfs.email}"]
        if self.app_session.efashion:
            lines.append(f"EFashion : {self.app_session.efashion.email}")
        self.status_label.setText("\n".join(lines))

    def _start_update_scheduler(self) -> None:
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(UPDATE_CHECK_INTERVAL_MS)
        self._update_timer.timeout.connect(self._check_app_update)
        self._update_timer.start()
        QTimer.singleShot(2_000, self._check_app_update)

    def _check_app_update(self) -> None:
        if self._update_thread is not None and self._update_thread.isRunning():
            return

        worker = AppUpdateWorker()
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        worker.available.connect(self._on_app_update_available)
        worker.failed.connect(self._on_app_update_failed)

        def _clear() -> None:
            if self._update_thread is thread:
                self._update_thread = None
                self._update_worker = None

        thread.finished.connect(_clear)
        self._update_thread = thread
        self._update_worker = worker
        thread.start()

    def _on_app_update_failed(self, message: str) -> None:
        self._append_log(f"Mise à jour app : {message}")
        if self._update_error_shown:
            return
        self._update_error_shown = True
        print(f"[auto-update] {message}", file=sys.stderr)

    def _on_app_update_available(
        self, version: str, file_path: str, _html_url: str
    ) -> None:
        if version == self._update_prompted_version:
            return
        self._update_prompted_version = version
        self._append_log(f"Nouvelle version {version} téléchargée : {file_path}")

        relocate_note = ""
        if is_frozen_app():
            target = resolve_update_target_path()
            current = current_install_path()
            if (
                target is not None
                and current is not None
                and target.resolve() != current.resolve()
            ):
                relocate_note = f"\n\nElle sera installée ici :\n{target}"

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Mise à jour disponible")
        box.setText(
            f"La version {version} est disponible "
            f"(vous êtes en {APP_VERSION}).\n\n"
            "L’application va se fermer, installer la nouvelle version, "
            f"puis se relancer.{relocate_note}"
        )
        box.setInformativeText(file_path)
        install_btn = box.addButton("Installer et relancer", QMessageBox.AcceptRole)
        box.addButton("Plus tard", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not install_btn:
            return

        if not is_frozen_app():
            QMessageBox.information(
                self,
                "Mise à jour",
                "L’installation automatique ne fonctionne que depuis "
                "l’application empaquetée (.app / .exe).\n\n"
                f"Le zip est prêt ici :\n{file_path}",
            )
            folder = QUrl.fromLocalFile(str(Path(file_path).resolve().parent))
            QDesktopServices.openUrl(folder)
            return

        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            apply_update_and_relaunch(Path(file_path))
        except AppUpdateError as exc:
            QMessageBox.critical(self, "Mise à jour", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        # Quit immédiat : sinon le script attend un PID qui ne meurt jamais.
        os._exit(0)

    def _append_log(self, message: str) -> None:
        return

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.fetch_button.setEnabled(not busy)
        self._update_send_button_state()
        self.logout_button.setEnabled(not busy)
        self.pfs_login_button.setEnabled(not busy)
        self.efashion_login_button.setEnabled(not busy)

    def _duplicates_check_blocking(self) -> bool:
        return (
            self._busy
            or self._pending_existing_check
            or self._pending_task in {"fetch_products", "check_existing_refs"}
            or not self._existing_refs_verified
        )

    def _selection_locked(self) -> bool:
        """Cases à cocher bloquées tant que la vérif doublons n'est pas terminée."""
        return not self._existing_refs_verified

    def _update_send_button_state(self) -> None:
        if not hasattr(self, "send_button"):
            return
        has_session = bool(
            self.app_session
            and self.app_session.efashion
            and self.app_session.efashion.access_token
        )
        blocking = self._duplicates_check_blocking()
        create_ok = (
            (not blocking)
            and bool(self.products)
            and bool(self.selected_ids)
            and has_session
        )
        update_ok = (
            (not blocking)
            and bool(self.products)
            and bool(self.selected_update_ids)
            and has_session
        )
        self.send_button.setEnabled(create_ok)
        self.send_button.style().unpolish(self.send_button)
        self.send_button.style().polish(self.send_button)
        self.send_button.update()
        if hasattr(self, "update_button"):
            self.update_button.setEnabled(update_ok)
            self.update_button.style().unpolish(self.update_button)
            self.update_button.style().polish(self.update_button)
            self.update_button.update()

    def _start_worker(self, worker: Worker) -> None:
        if self._thread and self._thread.isRunning():
            QMessageBox.warning(self, APP_NAME, "Une opération est déjà en cours.")
            return

        self._pending_task = worker.task
        self._worker = worker
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        worker.failed.connect(self._on_worker_failed)
        worker.log.connect(self._append_log)
        worker.progress.connect(self._on_progress)
        worker.pfs_login_done.connect(self._on_pfs_login_done)
        worker.efashion_step_done.connect(self._on_efashion_step_done)
        worker.products_ready.connect(self._on_products_ready)
        worker.existing_refs_ready.connect(self._on_existing_refs_ready)
        worker.send_done.connect(self._on_send_done)
        worker.update_done.connect(self._on_update_done)

        def _on_thread_finished() -> None:
            # Ne pas écraser un nouveau thread déjà démarré
            if self._thread is not thread:
                return
            self._worker = None
            self._thread = None
            finished_task = self._pending_task
            self._pending_task = ""
            self._set_busy(False)
            # Enchaîner la vérif doublons SEULEMENT après arrêt complet du thread
            if (
                self._pending_existing_check
                and finished_task != "check_existing_refs"
            ):
                QTimer.singleShot(0, self._start_existing_refs_check)

        thread.finished.connect(_on_thread_finished)

        self._thread = thread
        self._set_busy(True)
        thread.start()

    def _on_worker_failed(self, msg: str) -> None:
        QMessageBox.critical(self, APP_NAME, msg)
        if self._pending_task == "pfs_login":
            self.pfs_login_status.setText(msg)
            self.pfs_login_status.setStyleSheet("color: #b91c1c;")
        elif self._pending_task == "efashion_login":
            self.efashion_login_status.setText(msg)
            self.efashion_login_status.setStyleSheet("color: #b91c1c;")
        elif self._pending_task == "check_existing_refs":
            self._pending_existing_check = False
            self._existing_refs_verified = False
            self._append_log(f"Vérification doublons impossible : {msg}")
            self.summary_label.setText(
                f"{len(self.products)} produit(s) — vérification doublons impossible."
            )
            self._update_send_button_state()

    def _on_pfs_login(self) -> None:
        email = self.pfs_email_input.text().strip()
        password = self.pfs_password_input.text().strip()
        if not email or not password:
            self.pfs_login_status.setText("Email et mot de passe requis.")
            self.pfs_login_status.setStyleSheet("color: #b91c1c;")
            return

        self.pfs_login_status.setText("Connexion au compte source en cours…")
        self.pfs_login_status.setStyleSheet("color: #6b7280;")

        worker = Worker(
            task="pfs_login",
            app_session=self.app_session,
            store=self.session_store,
            email=email,
            password=password,
        )
        self._start_worker(worker)

    def _on_pfs_login_done(self) -> None:
        self.app_session = self.session_store.load()
        self.pfs_password_input.clear()
        self.pfs_login_status.setText("Connecté.")
        self.pfs_login_status.setStyleSheet("color: #15803d;")
        self._show_screen()

    def _on_efashion_login(self) -> None:
        email = self.efashion_email_input.text().strip()
        password = self.efashion_password_input.text().strip()
        if not email or not password:
            self.efashion_login_status.setText("Email et mot de passe EFashion requis.")
            self.efashion_login_status.setStyleSheet("color: #b91c1c;")
            return

        self.app_session = self.session_store.load()
        if not self.app_session:
            QMessageBox.warning(self, APP_NAME, "Connectez-vous au compte source d'abord.")
            return

        self.efashion_login_status.setText("Connexion EFashion en cours…")
        self.efashion_login_status.setStyleSheet("color: #6b7280;")

        worker = Worker(
            task="efashion_login",
            app_session=self.app_session,
            store=self.session_store,
            email=email,
            password=password,
        )
        self._start_worker(worker)

    def _on_efashion_step_done(self) -> None:
        self.app_session = self.session_store.load()
        self.efashion_password_input.clear()
        self.efashion_login_status.setText("Connecté.")
        self.efashion_login_status.setStyleSheet("color: #15803d;")
        self._show_screen()
        # Ne pas démarrer un 2e thread ici : le login tourne encore.
        if self.products:
            self._existing_refs_verified = False
            self._pending_existing_check = True
            self._update_send_button_state()

    def _on_logout(self) -> None:
        self.session_store.clear()
        self.app_session = None
        self.products = []
        self.displayed_create_products = []
        self.displayed_existing_products = []
        self.raw_pages = []
        self.raw_variant_pages = []
        self.selected_ids = set()
        self.selected_update_ids = set()
        self.existing_references = set()
        self._existing_refs_verified = False
        self._pending_existing_check = False
        self.pfs_password_input.clear()
        self.efashion_password_input.clear()
        if hasattr(self, "reference_filter_input"):
            self.reference_filter_input.clear()
        for checkbox_name in ("select_all_checkbox", "select_all_update_checkbox"):
            checkbox = getattr(self, checkbox_name, None)
            if checkbox is not None:
                checkbox.blockSignals(True)
                checkbox.setCheckState(Qt.Unchecked)
                checkbox.blockSignals(False)
        self.products_table.setRowCount(0)
        if hasattr(self, "existing_table"):
            self.existing_table.setRowCount(0)
        self.product_detail_box.clear()
        if hasattr(self, "image_layout"):
            self._clear_images()
        self.send_button.setEnabled(False)
        if hasattr(self, "update_button"):
            self.update_button.setEnabled(False)
        self.summary_label.setText("Chargement des produits en cours…")
        self.filter_status_label.setText("")
        self.progress_bar.setValue(0)
        self._show_screen()

    def _on_fetch_products(self) -> None:
        self.app_session = self.session_store.load()
        if not self.app_session or not self.app_session.pfs:
            QMessageBox.warning(self, APP_NAME, "Connectez-vous au compte source d'abord.")
            return

        self._existing_refs_verified = False
        self._pending_existing_check = False
        self.summary_label.setText("Récupération de vos produits…")
        self.progress_bar.setValue(0)
        self._update_send_button_state()

        worker = Worker(
            task="fetch_products",
            app_session=self.app_session,
        )
        self._start_worker(worker)

    def _on_products_ready(
        self, products: object, raw_pages: object, raw_variant_pages: object
    ) -> None:
        self.products = sorted(
            list(products),  # type: ignore[arg-type]
            key=lambda product: str(product.get("creation_date") or ""),
            reverse=True,
        )
        self.raw_pages = list(raw_pages)  # type: ignore[arg-type]
        self.raw_variant_pages = list(raw_variant_pages)  # type: ignore[arg-type]
        self.existing_references = set()
        self._existing_refs_verified = False
        self.selected_ids = set()
        self.selected_update_ids = set()
        for product in self.products:
            product["original_reference"] = self._product_reference(product)
        self._apply_reference_filter()
        with_variants = sum(
            1 for product in self.products if product.get("variants_loaded")
        )
        with_weight = sum(1 for product in self.products if product.get("weight"))
        with_pack = sum(1 for product in self.products if product.get("pack_label"))
        self.summary_label.setText(
            f"{len(self.products)} produit(s) actif(s) chargé(s) "
            f"({with_variants} avec variantes, {with_weight} avec poids, "
            f"{with_pack} avec paquet). "
            "Le détail sera enrichi à l'envoi."
        )
        self._update_send_button_state()
        self.progress_bar.setValue(100)
        self._append_log(
            f"Chargement rapide terminé : {len(self.products)} produit(s), "
            f"{with_variants} avec variantes, {with_weight} avec poids, "
            f"{with_pack} avec paquet."
        )
        # Ne pas démarrer un 2e thread ici : le fetch tourne encore.
        # La vérif doublons partira à la fin du thread (voir _start_worker).
        if (
            self.app_session
            and self.app_session.efashion
            and self.app_session.efashion.access_token
        ):
            self._pending_existing_check = True
            self._update_send_button_state()

    def _start_existing_refs_check(self) -> None:
        self.app_session = self.session_store.load()
        if (
            not self.app_session
            or not self.app_session.efashion
            or not self.app_session.efashion.access_token
            or not self.products
        ):
            self._pending_existing_check = False
            return
        if self._thread and self._thread.isRunning():
            self._pending_existing_check = True
            return

        self._pending_existing_check = False
        self._existing_refs_verified = False
        self.selected_ids = set()
        self.selected_update_ids = set()
        self.progress_bar.setRange(0, 0)
        self.summary_label.setText("Vérification des produits déjà présents…")
        self._update_send_button_state()
        self._apply_reference_filter()
        worker = Worker(
            task="check_existing_refs",
            app_session=self.app_session,
            products=self.products,
        )
        self._start_worker(worker)

    def _on_existing_refs_ready(self, existing: object) -> None:
        self.existing_references = {
            str(ref).strip() for ref in (existing or set()) if str(ref).strip()
        }
        for product in self.products:
            if not product.get("original_reference"):
                product["original_reference"] = self._product_reference(product)
            key = self._product_key(product)
            if self._is_already_on_catalog(product):
                self.selected_ids.discard(key)
            else:
                self.selected_update_ids.discard(key)

        self._existing_refs_verified = True
        self._apply_reference_filter()
        count = sum(1 for p in self.products if self._is_already_on_catalog(p))
        create_count = len(self.products) - count
        if count:
            self.summary_label.setText(
                f"{len(self.products)} produit(s) — {create_count} à créer, "
                f"{count} déjà en ligne (tableau du bas)."
            )
            self._append_log(
                f"{count} produit(s) déjà en ligne → disponibles pour mise à jour."
            )
        else:
            self.summary_label.setText(
                f"{len(self.products)} produit(s) — aucun déjà en ligne."
            )
            self._append_log("Aucune référence déjà présente sur le catalogue.")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self._update_send_button_state()

    @staticmethod
    def _product_reference(product: dict) -> str:
        return str(product.get("reference") or "").strip()

    def _is_already_on_catalog(self, product: dict) -> bool:
        reference = self._product_reference(product)
        return bool(reference) and reference in self.existing_references

    def _make_products_table(
        self,
        *,
        select_all_attr: str,
        on_select_all,
        on_changed,
        on_selected,
        min_height: int = 200,
    ) -> QTableWidget:
        table = QTableWidget(0, len(self._table_headers))
        table.setHorizontalHeaderLabels(self._table_headers)
        header = table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        table.setColumnWidth(0, 42)
        table.setColumnWidth(1, 140)
        table.setEditTriggers(
            QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed
        )
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setMinimumHeight(min_height)
        table.itemSelectionChanged.connect(on_selected)
        table.itemChanged.connect(on_changed)

        checkbox = QCheckBox(header)
        checkbox.setTristate(True)
        checkbox.setToolTip("Tout sélectionner")
        checkbox.clicked.connect(on_select_all)
        setattr(self, select_all_attr, checkbox)

        def reposition(*_args: object, t=table, c=checkbox) -> None:
            h = t.horizontalHeader()
            size = 18
            x = h.sectionViewportPosition(0) + max(0, (h.sectionSize(0) - size) // 2)
            y = max(0, (h.height() - size) // 2)
            c.setGeometry(x, y, size, size)
            c.raise_()

        header.sectionResized.connect(reposition)
        header.geometriesChanged.connect(reposition)
        QTimer.singleShot(0, reposition)
        return table

    def _fill_table(
        self,
        table: QTableWidget,
        products: list[dict],
        selected: set[str],
        *,
        allow_edit_reference: bool,
        select_all_checkbox: QCheckBox | None,
    ) -> None:
        self._updating_table = True
        table.setRowCount(len(products))
        selection_locked = self._selection_locked()
        locked_tip = "Attendez la fin de la vérification des doublons."
        rename_tip = "Double-cliquez pour modifier la référence avant création."

        for row, product in enumerate(products):
            check_item = QTableWidgetItem()
            if selection_locked:
                check_item.setFlags(Qt.ItemIsSelectable)
                check_item.setCheckState(Qt.Unchecked)
                check_item.setToolTip(locked_tip)
            else:
                check_item.setFlags(
                    Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable
                )
                checked = self._product_key(product) in selected
                check_item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            table.setItem(row, 0, check_item)

            values = product_table_row(product)
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if col == 0 and allow_edit_reference:
                    cell.setFlags(
                        Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable
                    )
                    cell.setToolTip(rename_tip)
                else:
                    cell.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                if not allow_edit_reference and col == 0:
                    cell.setForeground(QBrush(QColor("#b45309")))
                table.setItem(row, col + 1, cell)

        self._updating_table = False
        if products:
            table.selectRow(0)
        if select_all_checkbox is not None:
            select_all_checkbox.setEnabled(not selection_locked)
            select_all_checkbox.setToolTip(
                locked_tip if selection_locked else "Tout sélectionner"
            )
            keys = [self._product_key(p) for p in products if self._product_key(p)]
            selected_count = sum(1 for k in keys if k in selected)
            select_all_checkbox.blockSignals(True)
            if not keys or selected_count == 0:
                select_all_checkbox.setCheckState(Qt.Unchecked)
            elif selected_count == len(keys):
                select_all_checkbox.setCheckState(Qt.Checked)
            else:
                select_all_checkbox.setCheckState(Qt.PartiallyChecked)
            select_all_checkbox.blockSignals(False)

    @staticmethod
    def _product_key(product: dict) -> str:
        return str(product.get("id") or product.get("reference") or "")

    def _on_create_item_changed(self, item: QTableWidgetItem) -> None:
        self._on_table_item_changed(
            item,
            self.displayed_create_products,
            self.selected_ids,
            allow_edit_reference=True,
        )

    def _on_update_item_changed(self, item: QTableWidgetItem) -> None:
        self._on_table_item_changed(
            item,
            self.displayed_existing_products,
            self.selected_update_ids,
            allow_edit_reference=False,
        )

    def _on_table_item_changed(
        self,
        item: QTableWidgetItem,
        products: list[dict],
        selected: set[str],
        *,
        allow_edit_reference: bool,
    ) -> None:
        if self._updating_table:
            return
        row = item.row()
        if row < 0 or row >= len(products):
            return
        product = products[row]

        if item.column() == 1 and allow_edit_reference:
            self._on_reference_edited(product, item)
            return

        if item.column() != 0:
            return
        if self._selection_locked():
            self._updating_table = True
            item.setCheckState(Qt.Unchecked)
            self._updating_table = False
            return
        key = self._product_key(product)
        if not key:
            return
        if item.checkState() == Qt.Checked:
            selected.add(key)
        else:
            selected.discard(key)
        self._apply_reference_filter()
        self._update_selection_status()

    def _on_reference_edited(self, product: dict, item: QTableWidgetItem) -> None:
        new_ref = item.text().strip()
        old_ref = self._product_reference(product)
        if not product.get("original_reference"):
            product["original_reference"] = old_ref or new_ref

        if not new_ref:
            self._updating_table = True
            item.setText(old_ref or str(product.get("original_reference") or ""))
            self._updating_table = False
            QMessageBox.warning(self, APP_NAME, "La référence ne peut pas être vide.")
            return

        if new_ref == old_ref:
            return

        for other in self.products:
            if other is product:
                continue
            if self._product_reference(other) == new_ref:
                self._updating_table = True
                item.setText(old_ref)
                self._updating_table = False
                QMessageBox.warning(
                    self,
                    APP_NAME,
                    f"La référence « {new_ref} » est déjà utilisée dans la liste.",
                )
                return

        product["reference"] = new_ref
        was_blocked = old_ref in self.existing_references
        still_blocked = new_ref in self.existing_references
        key = self._product_key(product)

        if still_blocked:
            self.selected_ids.discard(key)
            self._append_log(
                f"Référence renommée {old_ref} → {new_ref} "
                "(toujours bloquée : existe déjà sur le catalogue)."
            )
        elif was_blocked:
            self.selected_update_ids.discard(key)
            self._append_log(
                f"Référence renommée {old_ref} → {new_ref} "
                "(passe dans « À créer »)."
            )
        else:
            self._append_log(f"Référence renommée {old_ref} → {new_ref}.")

        self._apply_reference_filter()
        self._update_selection_status()

    def _on_select_all_create_clicked(self, _checked: bool) -> None:
        self._toggle_select_all(self.displayed_create_products, self.selected_ids)

    def _on_select_all_update_clicked(self, _checked: bool) -> None:
        self._toggle_select_all(
            self.displayed_existing_products, self.selected_update_ids
        )

    def _toggle_select_all(self, products: list[dict], selected: set[str]) -> None:
        if self._selection_locked():
            self._apply_reference_filter()
            return
        keys = [self._product_key(p) for p in products if self._product_key(p)]
        all_selected = bool(keys) and all(k in selected for k in keys)
        if all_selected:
            for key in keys:
                selected.discard(key)
        else:
            selected.update(keys)
        self._apply_reference_filter()
        self._update_selection_status()

    def _update_selection_status(self) -> None:
        self.filter_status_label.setText(
            f"À créer : {len(self.displayed_create_products)} "
            f"(sel. {len(self.selected_ids)}) — "
            f"Déjà en ligne : {len(self.displayed_existing_products)} "
            f"(sel. {len(self.selected_update_ids)}) — "
            f"Total : {len(self.products)}"
        )
        self._update_send_button_state()

    def selected_products(self) -> list[dict]:
        return [
            product
            for product in self.products
            if self._product_key(product) in self.selected_ids
            and not self._is_already_on_catalog(product)
        ]

    def selected_update_products(self) -> list[dict]:
        return [
            product
            for product in self.products
            if self._product_key(product) in self.selected_update_ids
            and self._is_already_on_catalog(product)
        ]

    def _apply_reference_filter(self) -> None:
        needle = ""
        if hasattr(self, "reference_filter_input"):
            needle = self.reference_filter_input.text().strip().lower()
        if not needle:
            filtered = list(self.products)
        else:
            filtered = []
            for product in self.products:
                reference = self._product_reference(product).lower()
                labels = (
                    product.get("labels")
                    if isinstance(product.get("labels"), dict)
                    else {}
                )
                title = str(labels.get("fr") or labels.get("en") or "").lower()
                if needle in reference or needle in title:
                    filtered.append(product)

        if self._existing_refs_verified:
            create_list = [p for p in filtered if not self._is_already_on_catalog(p)]
            existing_list = [p for p in filtered if self._is_already_on_catalog(p)]
        else:
            create_list = filtered
            existing_list = []

        self.displayed_create_products = create_list
        self.displayed_existing_products = existing_list
        self._fill_table(
            self.products_table,
            create_list,
            self.selected_ids,
            allow_edit_reference=True,
            select_all_checkbox=getattr(self, "select_all_checkbox", None),
        )
        if hasattr(self, "existing_table"):
            self._fill_table(
                self.existing_table,
                existing_list,
                self.selected_update_ids,
                allow_edit_reference=False,
                select_all_checkbox=getattr(self, "select_all_update_checkbox", None),
            )
        self._update_selection_status()

    def _on_reference_filter_changed(self, _text: str) -> None:
        self._apply_reference_filter()

    def _on_create_product_selected(self) -> None:
        self._show_product_details(self.products_table, self.displayed_create_products)

    def _on_existing_product_selected(self) -> None:
        self._show_product_details(
            self.existing_table, self.displayed_existing_products
        )

    def _show_product_details(
        self, table: QTableWidget, products: list[dict]
    ) -> None:
        row = table.currentRow()
        if row < 0 or row >= len(products):
            self.product_detail_box.clear()
            self._load_product_images(None)
            return
        product = products[row]
        self.product_detail_box.setPlainText(format_product_details(product))
        self._load_product_images(product)

    def _clear_images(self) -> None:
        self._image_token += 1
        for reply in list(self._pending_replies):
            reply.abort()
            reply.deleteLater()
        self._pending_replies.clear()

        while self.image_layout.count():
            item = self.image_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.image_layout.addStretch()

    def _make_thumb(self, url: str, token: int) -> ClickableLabel:
        thumb = ClickableLabel()
        thumb.setFixedSize(THUMB_SIZE, THUMB_SIZE)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet(
            "border: 1px solid #e5e7eb; border-radius: 8px;"
            " background: #f9fafb; color: #9ca3af;"
        )
        thumb.setText("…")
        thumb.setToolTip(url)

        reply = self._net_manager.get(QNetworkRequest(QUrl(url)))
        self._pending_replies[reply] = (token, thumb)
        reply.finished.connect(lambda r=reply: self._on_image_downloaded(r))
        return thumb

    def _load_product_images(self, product: dict | None) -> None:
        self._clear_images()
        if not product:
            return

        groups = product_images_by_color(product)
        if not groups:
            placeholder = QLabel("Aucune photo")
            placeholder.setStyleSheet("color: #9ca3af;")
            self.image_layout.insertWidget(0, placeholder)
            return

        token = self._image_token
        for color_label, urls in groups:
            column = QWidget()
            column_layout = QVBoxLayout(column)
            column_layout.setContentsMargins(0, 0, 0, 0)
            column_layout.setSpacing(4)

            name = QLabel(color_label or "Photos")
            name.setStyleSheet("color: #374151; font-weight: 600;")
            column_layout.addWidget(name)

            thumbs_row = QHBoxLayout()
            thumbs_row.setContentsMargins(0, 0, 0, 0)
            thumbs_row.setSpacing(6)
            for url in urls[:8]:
                thumbs_row.addWidget(self._make_thumb(url, token))
            thumbs_row.addStretch()
            column_layout.addLayout(thumbs_row)

            self.image_layout.insertWidget(self.image_layout.count() - 1, column)

    def _on_image_downloaded(self, reply: QNetworkReply) -> None:
        entry = self._pending_replies.pop(reply, None)
        reply.deleteLater()
        if entry is None:
            return
        token, thumb = entry
        if token != self._image_token:
            return
        if reply.error() != QNetworkReply.NoError:
            thumb.setText("⚠")
            return

        data = reply.readAll()
        pixmap = QPixmap()
        if not pixmap.loadFromData(bytes(data.data())):
            thumb.setText("⚠")
            return

        thumb.setText("")
        thumb.setPixmap(
            pixmap.scaled(
                THUMB_SIZE - 6,
                THUMB_SIZE - 6,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )
        thumb.setCursor(Qt.PointingHandCursor)
        thumb.clicked.connect(lambda p=pixmap: self._open_full_image(p))

    def _open_full_image(self, pixmap: QPixmap) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Photo produit")
        dialog_layout = QVBoxLayout(dialog)
        dialog_layout.setContentsMargins(8, 8, 8, 8)

        label = QLabel()
        label.setAlignment(Qt.AlignCenter)
        screen = self.screen().availableGeometry() if self.screen() else None
        max_w = int(screen.width() * 0.8) if screen else 900
        max_h = int(screen.height() * 0.8) if screen else 700
        if pixmap.width() > max_w or pixmap.height() > max_h:
            display = pixmap.scaled(
                max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        else:
            display = pixmap
        label.setPixmap(display)
        dialog_layout.addWidget(label)
        dialog.exec()

    def _validate_selected_refs_before_send(self, selected: list[dict]) -> bool:
        """Re-vérifie les refs (après éventuel renommage) juste avant l'envoi."""
        assert self.app_session and self.app_session.efashion
        payload = [
            {"reference": self._product_reference(product)}
            for product in selected
            if self._product_reference(product)
        ]
        if not payload:
            return False
        try:
            with EfashionClient(self.app_session.efashion) as client:
                results = client.check_references_exists_batch(payload)
        except EfashionApiError as exc:
            QMessageBox.critical(
                self,
                APP_NAME,
                f"Impossible de vérifier les doublons avant envoi :\n{exc}",
            )
            return False

        blocked_refs = {ref for ref, exists in results.items() if exists}
        if not blocked_refs:
            return True

        self.existing_references.update(blocked_refs)
        for product in self.products:
            if self._product_reference(product) in blocked_refs:
                self.selected_ids.discard(self._product_key(product))
        self._apply_reference_filter()
        sample = ", ".join(sorted(blocked_refs)[:6])
        more = f" (+{len(blocked_refs) - 6})" if len(blocked_refs) > 6 else ""
        QMessageBox.warning(
            self,
            APP_NAME,
            "Ces références existent déjà sur le catalogue :\n"
            f"{sample}{more}\n\n"
            "Renommez-les (double-clic sur la colonne Référence) puis réessayez.",
        )
        return False

    def _on_send_to_efashion(self) -> None:
        if self._duplicates_check_blocking():
            QMessageBox.information(
                self,
                APP_NAME,
                "Attendez la fin de la vérification des doublons avant d'envoyer.",
            )
            return

        self.app_session = self.session_store.load()
        if not self.app_session or not self.app_session.efashion:
            QMessageBox.warning(
                self,
                APP_NAME,
                "Connectez-vous au compte catalogue avant d'envoyer des produits.",
            )
            return

        selected = self.selected_products()
        if not selected:
            QMessageBox.information(
                self,
                APP_NAME,
                "Cochez au moins un produit dans le tableau « À créer ».",
            )
            return

        if not self._validate_selected_refs_before_send(selected):
            return

        count = len(selected)
        reply = QMessageBox.question(
            self,
            "Créer les produits",
            (
                f"Êtes-vous sûr de vouloir envoyer {count} produit(s) "
                f"vers le catalogue ?\n\n"
                "Les produits seront importés sur EFashion avec leurs photos.\n"
                "• Actifs (READY_FOR_SALE) → en ligne, Visible coché\n"
                "• Brouillons source (NEW) → brouillon EFashion\n"
                "• Archivés / désactivés → hors ligne, Visible décoché\n"
                "• Rupture stock → stock 0 par couleur\n\n"
                "Vérifiez bien les informations avant de confirmer."
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        if not self._ensure_category_mappings(selected):
            return

        self.summary_label.setText(
            f"Enrichissement puis envoi de {count} produit(s)…"
        )
        self.progress_bar.setValue(0)
        self._append_log(
            f"Envoi EFashion lancé : enrichissement puis sync de "
            f"{count} produit(s) sélectionné(s)."
        )

        worker = Worker(
            task="send_to_efashion",
            app_session=self.app_session,
            products=selected,
        )
        self._start_worker(worker)

    def _on_update_on_efashion(self) -> None:
        if self._duplicates_check_blocking():
            QMessageBox.information(
                self,
                APP_NAME,
                "Attendez la fin de la vérification des doublons avant de mettre à jour.",
            )
            return

        self.app_session = self.session_store.load()
        if not self.app_session or not self.app_session.efashion:
            QMessageBox.warning(
                self,
                APP_NAME,
                "Connectez-vous au compte catalogue avant de mettre à jour.",
            )
            return

        selected = self.selected_update_products()
        if not selected:
            QMessageBox.information(
                self,
                APP_NAME,
                "Cochez au moins un produit dans le tableau « Déjà en ligne ».",
            )
            return

        count = len(selected)
        reply = QMessageBox.question(
            self,
            "Mettre à jour",
            (
                f"Mettre à jour {count} produit(s) déjà en ligne ?\n\n"
                "Refresh complet depuis le compte source :\n"
                "• prix, poids, marque, catégorie, collection, provenance, pack, "
                "déclinaison, dimensions, descriptions\n"
                "• nouvelles couleurs / variantes (+ photos)\n"
                "• refresh des photos (remplacement)\n"
                "• stock (rupture + quantités), visibilité, couleur principale\n"
                "• composition matières\n"
                "• couleurs disparues côté source → suppression EFashion\n"
                "• création des fiches manquantes\n\n"
                "Les données EFashion concernées seront écrasées / resynchronisées."
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        if not self._ensure_category_mappings(selected):
            return

        self.summary_label.setText(f"Mise à jour de {count} produit(s)…")
        self.progress_bar.setValue(0)
        self._append_log(f"Mise à jour EFashion lancée : {count} produit(s).")

        worker = Worker(
            task="update_on_efashion",
            app_session=self.app_session,
            products=selected,
        )
        self._start_worker(worker)

    def _ensure_category_mappings(self, products: list[dict]) -> bool:
        """Popup L1→L2→L3 pour les catégories source encore inconnues."""
        assert self.app_session and self.app_session.efashion
        store = CategoryMappingStore(
            id_vendeur=self.app_session.efashion.id_vendeur
        )
        missing = store.missing_keys_for_products(products)
        if not missing:
            return True

        self.summary_label.setText("Chargement des catégories EFashion…")
        QApplication.processEvents()
        try:
            with EfashionClient(self.app_session.efashion) as client:
                reference_data = client.get_reference_data()
        except EfashionApiError as exc:
            QMessageBox.critical(
                self,
                APP_NAME,
                f"Impossible de charger les catégories EFashion :\n{exc}",
            )
            return False

        l1, l2, l3 = category_options_from_reference(reference_data)
        for key, product in missing:
            pfs_label = pfs_category_label(product) or "Sans catégorie"
            gender = key.split("|", 1)[-1] if "|" in key else "*"
            dialog = CategoryMappingDialog(
                mapping_key=key,
                pfs_label=pfs_label,
                gender=gender,
                l1_options=l1,
                l2_options=l2,
                l3_options=l3,
                parent=self,
            )
            if dialog.exec() != QDialog.Accepted or not dialog.selected_entry:
                QMessageBox.information(
                    self,
                    APP_NAME,
                    "Envoi annulé : mapping catégorie incomplet.",
                )
                return False
            store.set_entry(key, dialog.selected_entry)
            self._append_log(
                f"Mapping catégorie enregistré : {key} → "
                f"{dialog.selected_entry.label} ({dialog.selected_entry.id})"
            )
        return True

    def _on_manage_category_mappings(self) -> None:
        self.app_session = self.session_store.load()
        if not self.app_session or not self.app_session.efashion:
            QMessageBox.warning(
                self,
                APP_NAME,
                "Connectez-vous au compte EFashion pour gérer les mappings.",
            )
            return

        store = CategoryMappingStore(
            id_vendeur=self.app_session.efashion.id_vendeur
        )
        dialog = CategoryMappingManagerDialog(
            store=store,
            id_vendeur=self.app_session.efashion.id_vendeur,
            efashion_session=self.app_session.efashion,
            parent=self,
        )
        dialog.exec()
        if dialog.changed and self.app_session and self.app_session.pfs:
            QTimer.singleShot(0, self._on_fetch_products)

    def _on_progress(self, done: int, total: int, message: str) -> None:
        if total > 0:
            if self.progress_bar.maximum() == 0:
                self.progress_bar.setRange(0, 100)
            value = int((done / total) * 100)
            self.progress_bar.setValue(value)
        else:
            self.progress_bar.setRange(0, 0)
        self.summary_label.setText(message)
        if (
            message.startswith("Page ")
            or message.startswith("listVariants")
            or message.startswith("Enrichissement")
            or message.startswith("Préparation de")
            or message.startswith("Préparation envoi")
            or message.startswith("Nouvelle tentative")
            or message.startswith("Envoi vers EFashion")
            or message.startswith("Photos S3")
            or message.startswith("Upload photos")
            or message.startswith("Mise en ligne")
            or message.startswith("Création des fiches")
            or message.startswith("Enrichissement")
            or message.startswith("Vérification")
            or "terminée" in message
            or "terminé" in message
            or done == total
            or (done > 0 and done % 100 == 0)
        ):
            self._append_log(message)

    def _on_send_done(self, count: int, message: str, sent_refs: object = None) -> None:
        self.progress_bar.setValue(100)
        sent = {
            str(ref).strip()
            for ref in (sent_refs or [])
            if str(ref).strip()
        }
        if sent:
            for product in self.products:
                if self._product_reference(product) in sent:
                    self.selected_ids.discard(self._product_key(product))
        else:
            self.selected_ids = set()
        self._apply_reference_filter()
        self._update_selection_status()
        self._append_log(message or f"Création terminée : {count} produit(s).")
        has_skipped = bool(message and "non envoyé" in message)
        if has_skipped:
            QMessageBox.warning(
                self,
                "Créer les produits",
                message
                or (
                    f"{count} produit(s) envoyé(s).\n\n"
                    "Certains produits sélectionnés n'ont pas pu être créés."
                ),
            )
        else:
            QMessageBox.information(
                self,
                "Créer les produits",
                message
                or (
                    f"{count} produit(s) envoyé(s) sur EFashion.\n\n"
                    "Actifs → en ligne (Visible coché)\n"
                    "NEW → brouillon\n"
                    "Archivés → hors ligne (Visible décoché)\n"
                    "Ruptures → stock 0 si détecté.\n\n"
                    "Merci de vérifier l'exactitude des informations si besoin."
                ),
            )
        if count > 0:
            QTimer.singleShot(0, self._on_fetch_products)

    def _on_update_done(self, count: int, message: str) -> None:
        self.progress_bar.setValue(100)
        self.selected_update_ids = set()
        self._apply_reference_filter()
        self._append_log(message or f"Mise à jour terminée : {count} produit(s).")
        QMessageBox.information(
            self,
            "Mettre à jour",
            message
            or (
                f"{count} produit(s) mis à jour sur le catalogue "
                "(prix, poids, catégorie, descriptions…)."
            ),
        )


def _set_macos_app_name(name: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = name
            info["CFBundleDisplayName"] = name
    except Exception:
        pass


def _set_macos_dock_icon(icon_path: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage

        image = NSImage.alloc().initByReferencingFile_(icon_path)
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        pass


def _app_icon() -> QIcon | None:
    for candidate in (ICON_PNG, ICON_ICNS):
        if candidate.exists():
            return QIcon(str(candidate))
    return None


def run_app() -> None:
    _set_macos_app_name(APP_NAME)
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName("Catalogue")
    QApplication.setDesktopFileName(APP_NAME)

    app = QApplication(sys.argv)
    app.setApplicationDisplayName(APP_NAME)

    icon = _app_icon()
    if icon is not None:
        app.setWindowIcon(icon)

    icns_path = ICON_ICNS if ICON_ICNS.exists() else ICON_PNG
    if icns_path.exists():
        _set_macos_dock_icon(str(icns_path))

    window = CatalogDesktopApp()
    window.show()
    sys.exit(app.exec())

import sys
import os
import logging
import platform
import subprocess
from datetime import datetime
import sqlite3
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QWidget, QPushButton, QFileDialog, QLabel,
    QTextEdit, QMenuBar, QMenu, QAction, QHBoxLayout, QProgressBar, QMessageBox, QDialog,
    QDialogButtonBox, QListWidget, QListWidgetItem, QAbstractItemView, QCheckBox, QScrollArea
)
from PyQt5.QtCore import Qt, QRunnable, QThreadPool, pyqtSignal, QObject
from PyQt5.QtGui import QTextCursor, QFont, QIcon

# Initialize logging
logging.basicConfig(level=logging.DEBUG, filename="error_log.txt", filemode="a",
                    format="%(asctime)s - %(levelname)s - %(message)s")

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

def get_current_user():
    """
    Retrieves the current logged-in user.
    """
    try:
        if platform.system() == "Windows":
            import win32api
            user = win32api.GetUserNameEx(win32api.NameSamCompatible)
            return user
        else:
            import getpass
            return getpass.getuser()
    except Exception as e:
        logging.error(f"Failed to get current user: {str(e)}")
        return "Unknown"


def get_owner(path):
    """
    Retrieves the owner of the specified file or directory.
    """
    try:
        if platform.system() == "Windows":
            import win32security
            sd = win32security.GetFileSecurity(path, win32security.OWNER_SECURITY_INFORMATION)
            owner_sid = sd.GetSecurityDescriptorOwner()
            name, domain, type = win32security.LookupAccountSid(None, owner_sid)
            return f"{domain}\\{name}"
        else:
            import pwd
            stat_info = os.stat(path)
            return pwd.getpwuid(stat_info.st_uid).pw_name
    except Exception as e:
        logging.error(f"Failed to get owner for {path}: {str(e)}")
        return "Unknown"


def set_owner(path, user):
    """
    Sets the owner of the specified file or directory to the specified user.
    """
    try:
        if platform.system() == "Windows":
            import win32security
            user_sid, domain, type = win32security.LookupAccountName(None, user)
            sd = win32security.GetFileSecurity(path, win32security.OWNER_SECURITY_INFORMATION)
            sd.SetSecurityDescriptorOwner(user_sid, False)
            win32security.SetFileSecurity(path, win32security.OWNER_SECURITY_INFORMATION, sd)
            return True, ""
        else:
            import pwd
            uid = pwd.getpwnam(user).pw_uid
            gid = pwd.getpwnam(user).pw_gid
            os.chown(path, uid, gid)
            return True, ""
    except Exception as e:
        logging.error(f"Failed to set owner for {path}: {str(e)}")
        return False, str(e)


def initialize_database():
    """
    Initializes the SQLite database to track ownership changes.
    """
    try:
        conn = sqlite3.connect('ownership_changes.db')
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ownership_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                original_owner TEXT NOT NULL,
                current_owner TEXT NOT NULL,
                operation_time DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Failed to initialize database: {str(e)}")


def record_change(path, original_owner, current_owner):
    """
    Records an ownership change in the database.
    """
    try:
        conn = sqlite3.connect('ownership_changes.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO ownership_changes (path, original_owner, current_owner)
            VALUES (?, ?, ?)
        ''', (path, original_owner, current_owner))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Failed to record change for {path}: {str(e)}")


def get_all_changes():
    """
    Retrieves all ownership changes from the database.
    """
    try:
        conn = sqlite3.connect('ownership_changes.db')
        cursor = conn.cursor()
        cursor.execute('SELECT id, path, original_owner, current_owner, operation_time FROM ownership_changes')
        records = cursor.fetchall()
        conn.close()
        return records
    except Exception as e:
        logging.error(f"Failed to retrieve changes from database: {str(e)}")
        return []


class WorkerSignals(QObject):
    """
    Defines the signals available from a running worker thread.
    """
    progress_update = pyqtSignal(int)  # Emitted with the current progress percentage
    log_message = pyqtSignal(str)       # Emitted with log messages
    counters_update = pyqtSignal(int, int, int)  # Changed, Unchanged, Errors
    status_update = pyqtSignal(str)    # Emitted with status updates
    error_occurred = pyqtSignal(str)   # Emitted when an error occurs
    ownership_change_finished = pyqtSignal()  # Emitted when ownership change is finished


class OwnershipCheckWorker(QRunnable):
    """
    Worker thread for checking ownership of files/folders.
    """
    def __init__(self, paths, signals, current_user):
        super().__init__()
        self.paths = paths
        self.signals = signals
        self.current_user = current_user

    def run(self):
        try:
            total = len(self.paths)
            changed = 0
            unchanged = 0
            errors = 0

            for idx, path in enumerate(self.paths, 1):
                try:
                    current_owner = get_owner(path)
                    self.signals.log_message.emit(f"Checked: {path} - Owner: {current_owner}")
                    if current_owner != self.current_user:
                        unchanged += 1  # Mark as needing change
                        # You can collect paths needing change here if required
                    else:
                        unchanged += 1
                except Exception as e:
                    logging.error(f"Error processing {path}: {str(e)}")
                    self.signals.log_message.emit(f"Error processing {path}: {str(e)}")
                    errors += 1

                # Update progress every 500 items or at the end
                if idx % 500 == 0 or idx == total:
                    progress = int((idx / total) * 100)
                    self.signals.progress_update.emit(progress)

            self.signals.counters_update.emit(changed, unchanged, errors)
            self.signals.status_update.emit("Ownership check completed.")
        except Exception as e:
            logging.error(f"Error in OwnershipCheckWorker: {str(e)}")
            self.signals.error_occurred.emit(f"Error in ownership check: {str(e)}")


class OwnershipChangeWorker(QRunnable):
    """
    Worker thread for changing ownership of selected files/folders.
    """
    def __init__(self, items, signals, current_user):
        super().__init__()
        self.items = items  # List of dicts with 'path', 'original_owner'
        self.signals = signals
        self.current_user = current_user

    def run(self):
        try:
            total = len(self.items)
            changed = 0
            unchanged = 0
            errors = 0

            for idx, item in enumerate(self.items, 1):
                path = item['path']
                original_owner = item['original_owner']
                try:
                    success, error = set_owner(path, self.current_user)
                    if success:
                        new_owner = get_owner(path)
                        self.signals.log_message.emit(f"Ownership changed for {path} to {new_owner}")
                        record_change(path, original_owner, new_owner)
                        changed += 1
                    else:
                        self.signals.log_message.emit(f"Failed to change owner for {path}: {error}")
                        errors += 1
                except Exception as e:
                    logging.error(f"Error changing ownership for {path}: {str(e)}")
                    self.signals.log_message.emit(f"Error changing ownership for {path}: {str(e)}")
                    errors += 1

                # Update progress every 500 items or at the end
                if idx % 500 == 0 or idx == total:
                    progress = int((idx / total) * 100)
                    self.signals.progress_update.emit(progress)

            self.signals.counters_update.emit(changed, unchanged, errors)
            self.signals.status_update.emit("Ownership change process completed.")
            self.signals.ownership_change_finished.emit()
        except Exception as e:
            logging.error(f"Error in OwnershipChangeWorker: {str(e)}")
            self.signals.error_occurred.emit(f"Error in ownership change: {str(e)}")


class RevertOwnershipWorker(QRunnable):
    """
    Worker thread for reverting ownership changes based on database records.
    """
    def __init__(self, records, signals):
        super().__init__()
        self.records = records  # List of tuples from the database
        self.signals = signals

    def run(self):
        try:
            total = len(self.records)
            changed = 0
            unchanged = 0
            errors = 0

            for idx, record in enumerate(self.records, 1):
                id, path, original_owner, current_owner, operation_time = record
                try:
                    success, error = set_owner(path, original_owner)
                    if success:
                        reverted_owner = get_owner(path)
                        self.signals.log_message.emit(f"Reverted ownership for {path} to {original_owner}")
                        # Remove the record from the database
                        conn = sqlite3.connect('ownership_changes.db')
                        cursor = conn.cursor()
                        cursor.execute('DELETE FROM ownership_changes WHERE id = ?', (id,))
                        conn.commit()
                        conn.close()
                        changed += 1
                    else:
                        self.signals.log_message.emit(f"Failed to revert owner for {path}: {error}")
                        errors += 1
                except Exception as e:
                    logging.error(f"Error reverting ownership for {path}: {str(e)}")
                    self.signals.log_message.emit(f"Error reverting ownership for {path}: {str(e)}")
                    errors += 1

                # Update progress every 500 items or at the end
                if idx % 500 == 0 or idx == total:
                    progress = int((idx / total) * 100)
                    self.signals.progress_update.emit(progress)

            self.signals.counters_update.emit(changed, unchanged, errors)
            self.signals.status_update.emit("Reversion process completed.")
            self.signals.ownership_change_finished.emit()
        except Exception as e:
            logging.error(f"Error in RevertOwnershipWorker: {str(e)}")
            self.signals.error_occurred.emit(f"Error in reverting ownership: {str(e)}")


class FinalReportDialog(QDialog):
    """
    Dialog to display the final report after operations.
    """
    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.report = report
        self.setWindowTitle("Operation Report 📄")
        self.setMinimumSize(700, 500)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()

        # Scroll Area for report details
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)

        # Window Icon
        icon = resource_path("app_icon.ico")
        self.setWindowIcon(QIcon(icon))

        # Changed Ownership Section
        if self.report["changed"] > 0:
            changed_label = QLabel("✅ Changed Ownership:")
            changed_label.setStyleSheet("font-weight: bold; font-size: 16px;")
            scroll_layout.addWidget(changed_label)
            scroll_layout.addWidget(QLabel(f"Total Changed: {self.report['changed']}"))
        else:
            scroll_layout.addWidget(QLabel("ℹ️ No ownership changes were made."))

        # Unchanged Ownership Section
        if self.report["unchanged"] > 0:
            unchanged_label = QLabel("ℹ️ Unchanged Ownership:")
            unchanged_label.setStyleSheet("font-weight: bold; font-size: 16px;")
            scroll_layout.addWidget(unchanged_label)
            scroll_layout.addWidget(QLabel(f"Total Unchanged: {self.report['unchanged']}"))

        # Errors Section
        if self.report["errors"] > 0:
            errors_label = QLabel("❌ Errors:")
            errors_label.setStyleSheet("font-weight: bold; font-size: 16px;")
            scroll_layout.addWidget(errors_label)
            scroll_layout.addWidget(QLabel(f"Total Errors: {self.report['errors']}"))
        else:
            success_label = QLabel("🎉 No errors were found. Operation completed successfully!")
            success_label.setStyleSheet("font-weight: bold; font-size: 16px; color: green;")
            scroll_layout.addWidget(success_label)

        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self.setLayout(layout)


class RevertChangesDialog(QDialog):
    """
    Dialog to select ownership changes to revert.
    """
    def __init__(self, records, parent=None):
        super().__init__(parent)
        self.records = records
        self.selected_records = []
        self.setWindowTitle("Revert Ownership Changes")
        self.setMinimumSize(600, 400)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()

        label = QLabel("Select the ownership changes you want to revert:")
        layout.addWidget(label)

        # Window Icon
        icon = resource_path("app_icon.ico")
        self.setWindowIcon(QIcon(icon))

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.MultiSelection)
        for record in self.records:
            id, path, original_owner, current_owner, operation_time = record
            item_text = f"{path} (Changed on {operation_time})"
            list_item = QListWidgetItem(item_text)
            list_item.setData(Qt.UserRole, record)
            self.list_widget.addItem(list_item)
        layout.addWidget(self.list_widget)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept_selection)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

    def accept_selection(self):
        selected_items = self.list_widget.selectedItems()
        if not selected_items:
            QMessageBox.information(self, "No Selection", "No changes selected for reversion.")
            return
        self.selected_records = [item.data(Qt.UserRole) for item in selected_items]
        self.accept()


class FileOwnerChanger(QMainWindow):
    """
    Main application window for the TSTP Permission Changer.
    """
    def __init__(self):
        super().__init__()
        try:
            initialize_database()
            self.selected_files = []
            self.selected_folders = []
            self.report_file = os.path.join(os.getcwd(), "ownership_report.txt")
            self.current_user = get_current_user()
            self.thread_pool = QThreadPool()
            self.init_ui()
            self.dark_mode = False
            logging.info("TSTP Permission Changer started.")
        except Exception as e:
            logging.error(f"Error during initialization: {str(e)}")
            QMessageBox.critical(self, "Initialization Error", f"An error occurred during initialization: {str(e)}")
            sys.exit(1)

    def init_ui(self):
        try:
            self.setWindowTitle("TSTP Permission Changer")
            self.setGeometry(200, 200, 800, 600)
            self.setMinimumWidth(700)
            self.setMinimumHeight(500)

            # Central widget
            self.central_widget = QWidget()
            self.setCentralWidget(self.central_widget)
            main_layout = QVBoxLayout()

            # Window Icon
            icon = resource_path("app_icon.ico")
            self.setWindowIcon(QIcon(icon))

            # Button Row
            btn_layout = QHBoxLayout()
            self.file_button = QPushButton("Select File(s) 📁")
            self.folder_button = QPushButton("Select Folder(s) 📂")
            self.save_button = QPushButton("Set Report Save Location 💾")
            self.check_button = QPushButton("Check Ownership Info 🔍")
            self.change_button = QPushButton("Change Permissions 🔧")
            self.revert_button = QPushButton("Revert Changes ↩️")
            btn_layout.addWidget(self.file_button)
            btn_layout.addWidget(self.folder_button)
            btn_layout.addWidget(self.save_button)
            btn_layout.addWidget(self.check_button)
            btn_layout.addWidget(self.change_button)
            btn_layout.addWidget(self.revert_button)
            main_layout.addLayout(btn_layout)
            # Ownership List
            ownership_header = QHBoxLayout()
            ownership_header.addWidget(QLabel("Ownership Status:"))
            self.select_all_checkbox = QCheckBox("Select All")
            self.select_all_checkbox.clicked.connect(self.toggle_select_all)
            ownership_header.addWidget(self.select_all_checkbox)
            ownership_header.addStretch()
            main_layout.addLayout(ownership_header)
            
            self.ownership_list = QListWidget()
            self.ownership_list.setSelectionMode(QAbstractItemView.MultiSelection)
            main_layout.addWidget(self.ownership_list)

            # Log Output
            main_layout.addWidget(QLabel("Log Output:"))
            self.log_display = QTextEdit()
            self.log_display.setReadOnly(True)
            self.log_display.setFont(QFont("Courier", 10))
            main_layout.addWidget(self.log_display)

            # Counters
            counters_layout = QHBoxLayout()
            self.changed_label = QLabel("Changed: 0")
            self.unchanged_label = QLabel("Unchanged: 0")
            self.errors_label = QLabel("Errors: 0")
            counters_layout.addWidget(self.changed_label)
            counters_layout.addWidget(self.unchanged_label)
            counters_layout.addWidget(self.errors_label)
            main_layout.addLayout(counters_layout)

            # Progress bar
            self.progress_bar = QProgressBar()
            self.progress_bar.setValue(0)
            main_layout.addWidget(self.progress_bar)

            # Status bar
            self.status_bar = self.statusBar()
            self.status_bar.showMessage("Ready")

            self.central_widget.setLayout(main_layout)

            # Menus
            menubar = QMenuBar(self)
            self.setMenuBar(menubar)

            # View menu
            view_menu = QMenu("View", self)
            dark_mode_action = QAction("Toggle Dark Mode 🌙", self, checkable=True)
            dark_mode_action.triggered.connect(self.toggle_dark_mode)
            view_menu.addAction(dark_mode_action)
            menubar.addMenu(view_menu)

            # Help menu
            help_menu = QMenu("Help", self)
            about_action = QAction("About ℹ️", self)
            tutorial_action = QAction("Tutorial 📖", self)
            donate_action = QAction("Donate 💖", self)
            about_action.triggered.connect(self.about_window)
            tutorial_action.triggered.connect(self.tutorial_window)
            donate_action.triggered.connect(self.donate_window)
            help_menu.addAction(about_action)
            help_menu.addAction(tutorial_action)
            help_menu.addAction(donate_action)
            menubar.addMenu(help_menu)

            # Connect buttons
            self.file_button.clicked.connect(self.select_files)
            self.folder_button.clicked.connect(self.select_folders)
            self.save_button.clicked.connect(self.set_report_location)
            self.check_button.clicked.connect(self.check_ownership_info)
            self.change_button.clicked.connect(self.change_permissions)
            self.revert_button.clicked.connect(self.initiate_revert_changes)

            # Disable Change Permissions button initially
            self.button_checker()
            
        except Exception as e:
            logging.error(f"Error during UI initialization: {str(e)}")
            QMessageBox.critical(self, "UI Initialization Error", f"An error occurred during UI setup: {str(e)}")
            sys.exit(1)

    def about_window(self):
        about_dialog = AboutDialog(self)
        about_dialog.exec_()

    def tutorial_window(self):
        tutorial_dialog = TutorialDialog(self)
        tutorial_dialog.exec_()

    def donate_window(self):
        donate_dialog = DonateDialog(self)
        donate_dialog.exec_()

    def toggle_select_all(self, checked):
        for i in range(self.ownership_list.count()):
            item = self.ownership_list.item(i)
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def open_link(self, url):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:
            logging.error(f"Failed to open link {url}: {str(e)}")
            QMessageBox.warning(self, "Link Error", f"Failed to open link: {str(e)}")

    def log_message(self, message):
        try:
            self.log_display.append(message)
            self.log_display.moveCursor(QTextCursor.End)
            logging.info(message)
        except Exception as e:
            logging.error(f"Failed to log message: {str(e)}")

    def select_files(self):
        try:
            files, _ = QFileDialog.getOpenFileNames(self, "Select File(s)")
            if files:
                self.selected_files.extend(files)
                self.log_message(f"Selected files: {', '.join(files)}")
                self.button_checker()
                self.revert_button.setEnabled(False)
        except Exception as e:
            logging.error(f"Error selecting files: {str(e)}")
            QMessageBox.warning(self, "File Selection Error", f"An error occurred while selecting files: {str(e)}")

    def select_folders(self):
        try:
            while True:
                folder = QFileDialog.getExistingDirectory(self, "Select Folder")
                if folder:
                    if folder not in self.selected_folders:
                        self.selected_folders.append(folder)
                        self.log_message(f"Selected folder: {folder}")
                    else:
                        self.log_message(f"Folder already selected: {folder}")
                    # Ask if the user wants to select another folder
                    reply = QMessageBox.question(
                        self, 'Select Another Folder',
                        "Do you want to select another folder?",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No
                    )
                    if reply == QMessageBox.No:
                        self.button_checker()
                        self.revert_button.setEnabled(False)
                        break
                else:
                    break
        except Exception as e:
            logging.error(f"Error selecting folders: {str(e)}")
            QMessageBox.warning(self, "Folder Selection Error", f"An error occurred while selecting folders: {str(e)}")

    def set_report_location(self):
        try:
            file_path, _ = QFileDialog.getSaveFileName(self, "Save Report As", "ownership_report.txt", "Text Files (*.txt)")
            if file_path:
                self.report_file = file_path
                self.log_message(f"Report save location set to: {self.report_file}")
        except Exception as e:
            logging.error(f"Error setting report location: {str(e)}")
            QMessageBox.warning(self, "Save Location Error", f"An error occurred while setting the report location: {str(e)}")

    def button_checker(self):
        """
        Enables/disables buttons based on whether files/folders are selected.
        """
        has_selections = bool(self.selected_files or self.selected_folders)
        
        # Change button only enabled if files/folders selected
        self.change_button.setEnabled(has_selections)
        
        # Save and check buttons depend on selections
        self.save_button.setEnabled(has_selections)
        self.check_button.setEnabled(has_selections)

    def toggle_dark_mode(self, checked):
        try:
            if checked:
                self.dark_mode = True
                self.setStyleSheet("""
                    QWidget {
                        background-color: #2E2E2E;
                        color: #FFFFFF;
                    }
                    QPushButton {
                        background-color: #4E4E4E;
                        color: #FFFFFF;
                        border: none;
                        padding: 8px;
                    }
                    QPushButton:hover {
                        background-color: #5E5E5E;
                    }
                    QTextEdit {
                        background-color: #3E3E3E;
                        color: #FFFFFF;
                    }
                    QLabel {
                        color: #FFFFFF;
                    }
                    QListWidget {
                        background-color: #3E3E3E;
                        color: #FFFFFF;
                    }
                    QMenuBar {
                        background-color: #2E2E2E;
                        color: #FFFFFF;
                    }
                    QMenuBar::item:selected {
                        background: #4E4E4E;
                    }
                    QMenu {
                        background-color: #2E2E2E;
                        color: #FFFFFF;
                    }
                    QMenu::item:selected {
                        background-color: #4E4E4E;
                    }
                    QProgressBar {
                        background-color: #3E3E3E;
                        color: #FFFFFF;
                        text-align: center;
                    }
                """)
            else:
                self.dark_mode = False
                self.setStyleSheet("")
        except Exception as e:
            logging.error(f"Error toggling dark mode: {str(e)}")
            QMessageBox.warning(self, "Dark Mode Error", f"An error occurred while toggling dark mode: {str(e)}")

    def check_ownership_info(self):
        try:
            # Collect all selected paths
            selected_paths = self.selected_files + self.selected_folders
            if not selected_paths:
                QMessageBox.warning(self, "No Selection", "Please select at least one file or folder to check.")
                return

            # Disable UI elements during processing
            self.file_button.setEnabled(False)
            self.folder_button.setEnabled(False)
            self.save_button.setEnabled(False)
            self.check_button.setEnabled(False)
            self.change_button.setEnabled(False)
            self.revert_button.setEnabled(False)

            # Reset counters and progress bar
            self.progress_bar.setValue(0)
            self.log_display.clear()
            self.status_bar.showMessage("Starting ownership check...")
            self.changed_label.setText("Changed: 0")
            self.unchanged_label.setText("Unchanged: 0")
            self.errors_label.setText("Errors: 0")

            # Clear the ownership list
            self.ownership_list.clear()

            # Gather all files and directories
            all_paths = []
            for path in selected_paths:
                if os.path.isfile(path):
                    all_paths.append(path)
                elif os.path.isdir(path):
                    for root, dirs, files in os.walk(path):
                        for d in dirs:
                            dir_path = os.path.join(root, d)
                            all_paths.append(dir_path)
                        for f in files:
                            file_path = os.path.join(root, f)
                            all_paths.append(file_path)

            total_items = len(all_paths)
            self.status_bar.showMessage(f"Total items to process: {total_items}")

            # Prepare signals
            self.signals = WorkerSignals()
            self.signals.progress_update.connect(self.update_progress)
            self.signals.log_message.connect(self.log_message)
            self.signals.counters_update.connect(self.update_counters)
            self.signals.status_update.connect(self.update_status)
            self.signals.error_occurred.connect(self.handle_error)

            # Multithreading with QThreadPool
            num_threads = min(4, QThreadPool.globalInstance().maxThreadCount())
            chunk_size = max(1, len(all_paths) // num_threads)
            chunks = [all_paths[i:i + chunk_size] for i in range(0, len(all_paths), chunk_size)]

            for chunk in chunks:
                worker = OwnershipCheckWorker(chunk, self.signals, self.current_user)
                self.thread_pool.start(worker)

            # Wait for all threads to finish
            self.thread_pool.waitForDone()

            # After checking, populate the ownership list
            self.populate_ownership_list(all_paths)

            self.status_bar.showMessage("Ownership check completed.")
            self.file_button.setEnabled(True)
            self.folder_button.setEnabled(True)
            self.save_button.setEnabled(True)
            self.check_button.setEnabled(True)
            self.change_button.setEnabled(True)
            self.revert_button.setEnabled(False)
            self.button_checker()
        except Exception as e:
            logging.error(f"Error during ownership check: {str(e)}")
            QMessageBox.critical(self, "Ownership Check Error", f"An error occurred during ownership check: {str(e)}")
            self.file_button.setEnabled(True)
            self.folder_button.setEnabled(True)
            self.save_button.setEnabled(True)
            self.check_button.setEnabled(True)
            self.change_button.setEnabled(True)
            self.revert_button.setEnabled(False)
            self.button_checker()

    def populate_ownership_list(self, all_paths):
        """
        Populates the QListWidget with ownership information.
        """
        try:
            for path in all_paths:
                current_owner = get_owner(path)
                item = QListWidgetItem(path)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                if current_owner != self.current_user:
                    item.setCheckState(Qt.Checked)  # Changed to Checked
                else:
                    item.setCheckState(Qt.Unchecked)  # Changed to Unchecked
                self.ownership_list.addItem(item)
        except Exception as e:
            logging.error(f"Error populating ownership list: {str(e)}")
            QMessageBox.warning(self, "List Population Error", f"An error occurred while populating the ownership list: {str(e)}")

    def change_permissions(self):
        """
        Changes ownership of selected files/folders.
        """
        try:
            # Gather selected items
            selected_items = []
            for index in range(self.ownership_list.count()):
                item = self.ownership_list.item(index)
                if item.checkState() == Qt.Checked:  # Changed to Checked
                    path = item.text()
                    original_owner = get_owner(path)
                    selected_items.append({'path': path, 'original_owner': original_owner})

            if not selected_items:
                QMessageBox.information(self, "No Selection", "No items selected for ownership change.")
                return

            # Confirm action
            reply = QMessageBox.question(
                self, 'Confirm Ownership Change',
                f"Are you sure you want to change ownership of {len(selected_items)} items to {self.current_user}?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return

            # Disable UI elements during processing
            self.file_button.setEnabled(False)
            self.folder_button.setEnabled(False)
            self.save_button.setEnabled(False)
            self.check_button.setEnabled(False)
            self.change_button.setEnabled(False)
            self.revert_button.setEnabled(False)

            # Reset counters and progress bar
            self.progress_bar.setValue(0)
            self.log_display.clear()
            self.status_bar.showMessage("Starting ownership change...")
            self.changed_label.setText("Changed: 0")
            self.unchanged_label.setText("Unchanged: 0")
            self.errors_label.setText("Errors: 0")

            # Prepare signals
            self.change_signals = WorkerSignals()
            self.change_signals.progress_update.connect(self.update_progress)
            self.change_signals.log_message.connect(self.log_message)
            self.change_signals.counters_update.connect(self.update_counters)
            self.change_signals.status_update.connect(self.update_status)
            self.change_signals.error_occurred.connect(self.handle_error)
            self.change_signals.ownership_change_finished.connect(self.show_final_report)

            # Start ownership change worker
            worker = OwnershipChangeWorker(selected_items, self.change_signals, self.current_user)
            self.thread_pool.start(worker)
            self.button_checker()
        except Exception as e:
            logging.error(f"Error during ownership change: {str(e)}")
            QMessageBox.critical(self, "Ownership Change Error", f"An error occurred during ownership change: {str(e)}")
            self.file_button.setEnabled(True)
            self.folder_button.setEnabled(True)
            self.save_button.setEnabled(True)
            self.check_button.setEnabled(True)
            self.change_button.setEnabled(True)
            self.revert_button.setEnabled(True)
            self.button_checker()

    def show_final_report(self):
        """
        Displays the final report after ownership changes.
        """
        try:
            report = {
                "changed": int(self.changed_label.text().split(": ")[1]),
                "unchanged": int(self.unchanged_label.text().split(": ")[1]), 
                "errors": int(self.errors_label.text().split(": ")[1])
            }
            dialog = FinalReportDialog(report, self)
            dialog.exec_()

            # Re-enable UI elements
            self.file_button.setEnabled(True)
            self.folder_button.setEnabled(True)
            self.save_button.setEnabled(True)
            self.check_button.setEnabled(True)
            self.change_button.setEnabled(True)
            self.revert_button.setEnabled(True)
        except Exception as e:
            logging.error(f"Error showing final report: {str(e)}")
            QMessageBox.warning(self, "Report Error", f"An error occurred while displaying the final report: {str(e)}")
            self.file_button.setEnabled(True)
            self.folder_button.setEnabled(True)
            self.save_button.setEnabled(True)
            self.check_button.setEnabled(True)
            self.change_button.setEnabled(True)
            self.revert_button.setEnabled(True)

    def update_progress(self, value):
        """
        Updates the progress bar.
        """
        try:
            self.progress_bar.setValue(value)
        except Exception as e:
            logging.error(f"Error updating progress bar: {str(e)}")

    def update_counters(self, changed, unchanged, errors):
        """
        Updates the Changed, Unchanged, and Errors counters.
        """
        try:
            self.changed_label.setText(f"Changed: {changed}")
            self.unchanged_label.setText(f"Unchanged: {unchanged}")
            self.errors_label.setText(f"Errors: {errors}")
        except Exception as e:
            logging.error(f"Error updating counters: {str(e)}")

    def update_status(self, message):
        """
        Updates the status bar.
        """
        try:
            self.status_bar.showMessage(message)
        except Exception as e:
            logging.error(f"Error updating status bar: {str(e)}")

    def handle_error(self, message):
        """
        Handles errors by logging and displaying a message box.
        """
        logging.error(message)
        QMessageBox.warning(self, "Error", message)

    def initiate_revert_changes(self):
        """
        Initiates the reversion of ownership changes based on database records.
        """
        try:
            records = get_all_changes()
            if not records:
                QMessageBox.information(self, "No Changes", "There are no ownership changes to revert.")
                return

            # Show dialog to select changes to revert
            dialog = RevertChangesDialog(records, self)
            result = dialog.exec_()

            if dialog.selected_records:
                # Disable UI elements during processing
                self.file_button.setEnabled(False)
                self.folder_button.setEnabled(False)
                self.save_button.setEnabled(False)
                self.check_button.setEnabled(False)
                self.change_button.setEnabled(False)
                self.revert_button.setEnabled(False)

                # Reset counters and progress bar
                self.progress_bar.setValue(0)
                self.log_display.clear()
                self.status_bar.showMessage("Starting reversion of ownership changes...")
                self.changed_label.setText("Changed: 0")
                self.unchanged_label.setText("Unchanged: 0")
                self.errors_label.setText("Errors: 0")

                # Prepare signals
                self.revert_signals = WorkerSignals()
                self.revert_signals.progress_update.connect(self.update_progress)
                self.revert_signals.log_message.connect(self.log_message)
                self.revert_signals.counters_update.connect(self.update_counters)
                self.revert_signals.status_update.connect(self.update_status)
                self.revert_signals.error_occurred.connect(self.handle_error)
                self.revert_signals.ownership_change_finished.connect(self.show_final_report)

                # Start revert worker
                worker = RevertOwnershipWorker(dialog.selected_records, self.revert_signals)
                self.thread_pool.start(worker)
        except Exception as e:
            logging.error(f"Error initiating revert changes: {str(e)}")
            QMessageBox.warning(self, "Revert Error", f"An error occurred while initiating revert: {str(e)}")

class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About TSTP Permission Changer")
        self.setMinimumWidth(800)
        self.setMinimumHeight(600)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Window Icon
        icon = resource_path("app_icon.ico")
        self.setWindowIcon(QIcon(icon))

        # Title Section
        title = QLabel("TSTP Permission Changer")
        title.setStyleSheet("""
            font-size: 32px;
            font-weight: bold;
            color: palette(windowText);
            margin: 20px;
        """)
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        version = QLabel("Version 1.0.0 Professional")
        version.setStyleSheet("""
            font-size: 16px;
            color: palette(windowText);
            font-style: italic;
            margin-bottom: 20px;
        """)
        version.setAlignment(Qt.AlignCenter)
        layout.addWidget(version)

        # Description Card
        desc_card = QWidget()
        desc_card.setStyleSheet("""
            QWidget {
                background-color: palette(window);
                border: 1px solid palette(mid);
                border-radius: 10px;
                padding: 20px;
            }
        """)
        desc_layout = QVBoxLayout()
        desc = QLabel(
            "<p style='font-size: 14px; line-height: 1.6; color: palette(windowText);'>"
            "TSTP Permission Changer is a sophisticated utility designed for seamless management "
            "of file and folder ownership across your system. Built with security and ease-of-use "
            "in mind, it offers:</p>"
            "<ul style='margin-top: 10px; color: palette(windowText);'>"
            "<li>Intuitive graphical interface for permission management</li>"
            "<li>Comprehensive logging and audit trail capabilities</li>"
            "<li>Advanced batch processing features</li>"
            "<li>Secure ownership verification system</li>"
            "<li>One-click reversion functionality</li>"
            "</ul>"
        )
        desc.setWordWrap(True)
        desc_layout.addWidget(desc)
        desc_card.setLayout(desc_layout)
        layout.addWidget(desc_card)

        # Company Info Card
        company_card = QWidget()
        company_card.setStyleSheet("""
            QWidget {
                background-color: palette(window);
                border: 1px solid palette(mid);
                border-radius: 10px;
                padding: 20px;
                margin-top: 20px;
            }
            QLabel {
                color: palette(windowText);
            }
        """)
        company_layout = QVBoxLayout()
        company = QLabel(
            "<div style='text-align: center;'>"
            "<h2 style='color: palette(windowText);'>The Solutions To Problems, LLC</h2>"
            "<p style='font-size: 14px; margin: 10px 0; color: palette(windowText);'>"
            "Transforming complex technical challenges into elegant solutions</p>"
            "</div>"
        )
        company_layout.addWidget(company)
        company_card.setLayout(company_layout)
        layout.addWidget(company_card)

        # Copyright Info
        copyright = QLabel("© 2024 The Solutions To Problems, LLC. All rights reserved.")
        copyright.setStyleSheet("""
            color: palette(windowText);
            font-size: 12px;
            margin-top: 10px;
        """)
        copyright.setAlignment(Qt.AlignCenter)
        layout.addWidget(copyright)

        # Button Row
        button_layout = QHBoxLayout()
        
        website_btn = QPushButton("Visit Website")
        support_btn = QPushButton("Contact Support") 
        docs_btn = QPushButton("Documentation")
        close_btn = QPushButton("Close")

        for btn in [website_btn, support_btn, docs_btn, close_btn]:
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #2E86C1;
                    color: white;
                    border: none;
                    padding: 10px 20px;
                    border-radius: 5px;
                    font-size: 14px;
                    min-width: 120px;
                }
                QPushButton:hover {
                    background-color: #21618C;
                }
                QPushButton:pressed {
                    background-color: #1B4F72;
                }
            """)
            button_layout.addWidget(btn)

        website_btn.clicked.connect(lambda: self.parent().open_link("https://tstp.xyz"))
        support_btn.clicked.connect(lambda: self.parent().open_link("mailto:support@tstp.xyz"))
        docs_btn.clicked.connect(lambda: self.parent().open_link("https://tstp.xyz/software/permission-changer"))
        close_btn.clicked.connect(self.close)

        layout.addLayout(button_layout)

class TutorialDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tutorial")
        self.setMinimumSize(900, 700)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout()
        content.setLayout(content_layout)

        # Window Icon
        icon = resource_path("app_icon.ico")
        self.setWindowIcon(QIcon(icon))

        sections = [
            ("Step 1: Getting Started 🚀", [
                ("Selecting Your Files", [
                    "• Click 'Select Files' to choose specific files you want to change ownership for",
                    "• Use 'Select Folders' to process entire directories at once - useful for bulk changes",
                    "• The program will scan and list all files/folders you've selected",
                    "Why: When files are created by another user or copied from elsewhere, you may need ownership to modify them"
                ]),
                ("Setting Up the Process", [
                    "• Choose a 'Save Location' for the change report - this creates an audit trail of all changes",
                    "• Click 'Check Ownership' to analyze current ownership status",
                    "• The program will display current owner information for each item",
                    "Why: It's important to review ownership before making changes to avoid unintended modifications"
                ])
            ]),
            ("Step 2: Understanding the Results 🔍", [
                ("Ownership Display", [
                    "• Files/folders will be listed with their current owners",
                    "• Items not owned by you are automatically checked for convenience",
                    "• The program highlights items that might need ownership changes",
                    "Why: This helps you quickly identify which items need attention"
                ]),
                ("Selection Options", [
                    "• Use individual checkboxes to fine-tune your selection",
                    "• 'Select All' quickly targets everything - useful for bulk operations",
                    "• You can uncheck items you want to leave unchanged",
                    "Why: Granular control ensures you only change what you intend to"
                ])
            ]),
            ("Step 3: Making Changes ⚡", [
                ("Initiating Changes", [
                    "• Review your selections carefully before proceeding",
                    "• Click 'Change Permissions' to begin the ownership transfer",
                    "• Confirm the action in the verification dialog",
                    "Why: Ownership changes are significant and should be intentional"
                ]),
                ("During the Process", [
                    "• The program will attempt to change ownership of each selected item",
                    "• Each successful change is logged and recorded in the database",
                    "• Failed changes are noted with specific error messages",
                    "Why: Tracking changes enables you to monitor progress and troubleshoot issues"
                ])
            ]),
            ("Step 4: Monitoring Progress 📊", [
                ("Real-time Updates", [
                    "• Watch the progress bar for overall completion status",
                    "• The log display shows detailed messages for each action",
                    "• Counters track successful changes, skipped items, and errors",
                    "Why: Real-time feedback helps you ensure the process is working as expected"
                ]),
                ("Understanding Results", [
                    "• Green messages indicate successful ownership changes",
                    "• Yellow messages show skipped or unchanged items",
                    "• Red messages highlight errors or permission issues",
                    "Why: Color-coding helps quickly identify any problems that need attention"
                ])
            ]),
            ("Step 5: Reverting Changes ↩️", [
                ("When to Revert", [
                    "• Use this if ownership changes caused unexpected issues",
                    "• Helpful when testing access permissions",
                    "• Allows you to undo recent changes safely",
                    "Why: Having a 'safety net' lets you experiment without permanent consequences"
                ]),
                ("Reversion Process", [
                    "• Click 'Revert Changes' to see a list of recent modifications",
                    "• Select specific changes you want to undo",
                    "• The program will restore original ownership settings",
                    "Why: Selective reversion gives you precise control over undoing changes"
                ])
            ])
        ]

        # Add title at the top
        main_title = QLabel("Complete Guide to TSTP Permission Changer")
        main_title.setStyleSheet("""
            font-size: 24px;
            font-weight: bold;
            color: palette(text);
            padding: 20px;
            qproperty-alignment: AlignCenter;
        """)
        content_layout.addWidget(main_title)

        for section_title, subsections in sections:
            section = QWidget()
            section_layout = QVBoxLayout()
            
            # Section Title
            title_label = QLabel(section_title)
            title_label.setStyleSheet("""
                font-size: 20px;
                font-weight: bold;
                color: palette(text);
                padding: 15px;
                background-color: palette(dark);
                border-radius: 8px;
                margin: 10px 0px;
            """)
            section_layout.addWidget(title_label)

            for subtitle, items in subsections:
                # Subsection Title
                subtitle_label = QLabel(subtitle)
                subtitle_label.setStyleSheet("""
                    font-size: 16px;
                    font-weight: bold;
                    color: palette(text);
                    padding: 10px;
                    margin-left: 15px;
                """)
                section_layout.addWidget(subtitle_label)

                # Content Widget
                content_widget = QWidget()
                content_widget.setStyleSheet("""
                    color: palette(text);
                    background-color: palette(base);
                    border: 1px solid palette(mid);
                    border-radius: 8px;
                    padding: 15px;
                    margin: 5px 15px 15px 15px;
                """)
                
                items_layout = QVBoxLayout()
                for item in items:
                    item_label = QLabel(item)
                    item_label.setWordWrap(True)
                    if item.startswith("Why:"):
                        item_label.setStyleSheet("""
                            color: palette(highlight);
                            font-style: italic;
                            padding: 5px;
                            margin-top: 5px;
                        """)
                    else:
                        item_label.setStyleSheet("""
                            color: palette(text);
                            padding: 3px;
                        """)
                    items_layout.addWidget(item_label)
                
                content_widget.setLayout(items_layout)
                section_layout.addWidget(content_widget)
            
            section.setLayout(section_layout)
            content_layout.addWidget(section)

        scroll.setWidget(content)
        layout.addWidget(scroll)

        # Close Button with improved styling
        close_btn = QPushButton("Close Tutorial")
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: palette(button);
                color: palette(button-text);
                padding: 10px;
                border-radius: 5px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: palette(highlight);
            }
            QPushButton:disabled {
                background-color: palette(dark);
                color: palette(disabled-text);
            }
        """)
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

class DonateDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Support Our Work 💖")
        self.setMinimumWidth(600)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        self.setLayout(layout)

        # Title
        title = QLabel("Support TSTP Permission Changer")
        title.setStyleSheet("font-size: 24px; font-weight: bold; margin: 20px;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        # Window Icon
        icon = resource_path("app_icon.ico")
        self.setWindowIcon(QIcon(icon))

        # Message
        message = QLabel(
            "Your support helps us continue developing and maintaining this tool. "
            "By donating, you're helping us create better solutions for file management "
            "and enabling us to add new features and improvements. Every donation, no matter "
            "the size, makes a real difference in our ability to maintain and enhance this software."
        )
        message.setWordWrap(True)
        message.setStyleSheet("margin: 20px; font-size: 14px;")
        layout.addWidget(message)

        # Impact Section
        impact = QLabel(
            "🎯 Your donation will help:\n"
            "• Maintain and improve the software\n"
            "• Develop new features and enhancements\n"
            "• Provide better support and documentation\n"
            "• Create more helpful tools for the community\n"
            "• Keep the software free and open source\n"
            "• Fund server costs and development resources"
        )
        impact.setStyleSheet("margin: 20px; font-size: 14px;")
        layout.addWidget(impact)

        # Benefits Section
        benefits = QLabel(
            "✨ Donor Benefits:\n"
            "• Priority support via email\n"
            "• Early access to new features\n" 
            "• Your name in our contributors list\n"
            "• Input on future development priorities"
        )
        benefits.setStyleSheet("margin: 20px; font-size: 14px;")
        layout.addWidget(benefits)

        # Donation Button
        donate_btn = QPushButton("Make a Donation 💝")
        donate_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                padding: 15px;
                border-radius: 8px;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
        """)
        donate_btn.clicked.connect(self.handle_donation)
        layout.addWidget(donate_btn)

        # Close Button
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

    def handle_donation(self):
        # Open initial donation page
        self.parent().open_link("https://tstp.xyz/donate")
        
        # Create timer to open PayPal after 2 seconds
        from PyQt5.QtCore import QTimer
        timer = QTimer(self)
        timer.singleShot(2000, lambda: self.parent().open_link(
            "https://www.paypal.com/donate/?hosted_button_id=RAAYNUTMHPQQN"
        ))

# Main function
def main():
    try:
        app = QApplication(sys.argv)
        window = FileOwnerChanger()
        window.show()
        sys.exit(app.exec_())
    except Exception as e:
        logging.critical(f"Fatal error: {str(e)}")
        QMessageBox.critical(None, "Fatal Error", f"A fatal error occurred: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()

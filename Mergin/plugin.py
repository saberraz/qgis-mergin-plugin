# -*- coding: utf-8 -*-

# GPLv3 license
# Copyright Lutra Consulting Limited

import sip
import os
import shutil
import posixpath
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsApplication,
    QgsDataItem,
    QgsDataCollectionItem,
    QgsErrorItem,
    QgsExpressionContextUtils,
    QgsDataItemProvider,
    QgsDataProvider,
    QgsProject,
    QgsProviderRegistry,
)
from qgis.utils import iface
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox, QApplication
from qgis.PyQt.QtCore import QSettings, Qt
from urllib.error import URLError

from .configuration_dialog import ConfigurationDialog
from .create_project_wizard import NewMerginProjectWizard
from .clone_project_dialog import CloneProjectDialog
from .projects_manager import MerginProjectsManager
from .sync_dialog import SyncDialog
from .utils import (
    ClientError,
    LoginError,
    create_mergin_client,
    find_qgis_files,
    icon_path,
    get_mergin_auth,
    login_error_message,
    mergin_project_local_path,
    same_dir,
    send_logs,
    unhandled_exception_message,
    unsaved_project_check,
    remove_project_variables,
)

from .mergin.merginproject import MerginProject


class MerginPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.data_item_provider = None
        self.actions = []
        self.actions_always_on = []
        self.menu = "Mergin Plugin"
        self.mergin_proj_dir = None
        self.manager = None
        self.toolbar = self.iface.addToolBar("Mergin Toolbar")
        self.toolbar.setToolTip("Mergin Toolbar")

        self.iface.projectRead.connect(self.on_qgis_project_changed)
        self.iface.newProjectCreated.connect(self.on_qgis_project_changed)
        QgsProject.instance().projectSaved.connect(self.on_qgis_project_changed)

        settings = QSettings()
        QgsExpressionContextUtils.setGlobalVariable("mergin_username", settings.value("Mergin/username", ""))
        QgsExpressionContextUtils.setGlobalVariable("mergin_url", settings.value("Mergin/server", ""))

    def initGui(self):
        # This is a quick fix for a bad crasher for users that have set up master password for their
        # storage of authentication configurations. What would happen is that in a worker thread,
        # QGIS browser model would start populating Mergin data items which would want to query Mergin
        # server and thus request auth info - but as this would be done in a background thread,
        # things will get horribly wrong when QGIS tries to display GUI and the app would crash.
        # Triggering auth request to QGIS auth framework already at this point will make sure that
        # the dialog asking for master password is started from the main thread -> no crash.
        get_mergin_auth()

        self.data_item_provider = DataItemProvider()
        QgsApplication.instance().dataItemProviderRegistry().addProvider(self.data_item_provider)
        # related to https://github.com/lutraconsulting/qgis-mergin-plugin/issues/3
        # if self.iface.browserModel().initialized():
        #     self.iface.browserModel().reload()
        try:
            mc = create_mergin_client()
            self.manager = MerginProjectsManager(mc)
        except ClientError:
            pass

        self.add_action(
            "mergin_configure.svg",
            text="Configure Mergin Plugin",
            callback=self.configure,
            add_to_menu=True,
            add_to_toolbar=self.toolbar,
        )
        self.add_action(
            "mergin_new_project.svg",
            text="Create Mergin Project",
            callback=self.create_new_project,
            add_to_menu=False,
            add_to_toolbar=self.toolbar,
            enabled=False,
            always_on=False,
        )
        self.add_action(
            "mergin_project_status.svg",
            text="Mergin Project Status",
            callback=self.current_project_status,
            add_to_menu=False,
            add_to_toolbar=self.toolbar,
            enabled=False,
            always_on=False,
        )
        self.add_action(
            "mergin_project_sync.svg",
            text="Synchronise Mergin Project",
            callback=self.current_project_sync,
            add_to_menu=False,
            add_to_toolbar=self.toolbar,
            enabled=False,
            always_on=False,
        )

    def add_action(
        self,
        icon_name,
        callback=None,
        text="",
        enabled=True,
        add_to_menu=False,
        add_to_toolbar=None,
        status_tip=None,
        whats_this=None,
        checkable=False,
        checked=False,
        always_on=True,
    ):

        icon = QIcon(icon_path(icon_name))
        action = QAction(icon, text, self.iface.mainWindow())
        action.triggered.connect(callback)
        action.setCheckable(checkable)
        action.setChecked(checked)
        action.setEnabled(enabled)

        if status_tip is not None:
            action.setStatusTip(status_tip)
        if whats_this is not None:
            action.setWhatsThis(whats_this)
        if add_to_toolbar is not None:
            add_to_toolbar.addAction(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)

        self.actions.append(action)
        if always_on:
            self.actions_always_on.append(text)
        return action

    def on_config_changed(self):
        """Called when plugin config (connection settings) were changed."""
        self.enable_toolbar_actions()

    def connect_provider_root_item(self):
        """Set the connection for Mergin config changes."""
        if self.data_item_provider.root_item is not None:
            # first try to disconnect the root item signal
            self.disconnect_provider_root_item()
            self.data_item_provider.root_item.config_changed.connect(self.on_config_changed)
            self.data_item_provider.root_item.local_project_removed.connect(self.on_config_changed)

    def disconnect_provider_root_item(self):
        try:
            self.data_item_provider.root_item.config_changed.disconnect(self.on_config_changed)
            self.data_item_provider.root_item.local_project_removed.disconnect(self.on_config_changed)
        except (TypeError, AttributeError):
            pass

    def enable_toolbar_actions(self, enable=None):
        """Check current project and set Mergin Toolbar icons enabled accordingly."""
        if enable is None:
            enable = mergin_project_local_path() is not None
        if self.data_item_provider.root_item.mc is None:
            enable = False
        for action in self.toolbar.actions():
            if action.text() in self.actions_always_on:
                action.setEnabled(True)
            elif action.text() == "Create Mergin Project":
                can_create = self.data_item_provider.root_item.mc is not None and self.manager is not None
                action.setEnabled(can_create)
            else:
                action.setEnabled(enable)

    def configure(self):
        """Open plugin configuration dialog."""
        if self.data_item_provider.root_item is None:
            QMessageBox.warning(
                None,
                "Mergin Plugin",
                "Mergin plugin first use requires QGIS restart.",
            )
            return
        self.data_item_provider.root_item.configure()

    def create_new_project(self):
        """Open new Mergin project creation dialog."""
        self.data_item_provider.root_item.show_create_project_dialog()

    def current_project_status(self):
        """Show Mergin project status/validation dialog."""
        self.manager.project_status(self.mergin_proj_dir)

    def current_project_sync(self):
        """Synchronise current Mergin project."""
        self.manager.sync_project(self.mergin_proj_dir)

    def on_qgis_project_changed(self):
        """
        Called when QGIS project is created or (re)loaded. Sets QGIS project related Mergin variables.
        If a loaded project is not a Mergin project, there are no Mergin variables by default.
        If a loaded project is invalid - doesnt have metadata, Mergin variables are removed.
        """
        self.connect_provider_root_item()
        self.enable_toolbar_actions(enable=False)
        self.mergin_proj_dir = mergin_project_local_path()
        if self.mergin_proj_dir is not None:
            self.enable_toolbar_actions()

    def unload(self):
        # Disconnect Mergin related signals
        self.iface.projectRead.disconnect(self.on_qgis_project_changed)
        self.iface.newProjectCreated.disconnect(self.on_qgis_project_changed)
        QgsProject.instance().projectSaved.disconnect(self.on_qgis_project_changed)
        self.disconnect_provider_root_item()

        remove_project_variables()
        QgsExpressionContextUtils.removeGlobalVariable("mergin_username")
        QgsExpressionContextUtils.removeGlobalVariable("mergin_url")
        QgsApplication.instance().dataItemProviderRegistry().removeProvider(self.data_item_provider)
        self.data_item_provider = None
        # this is crashing qgis on exit
        # self.iface.browserModel().reload()

        for action in self.actions:
            self.iface.removePluginMenu(self.menu, action)
            self.iface.removeToolBarIcon(action)
        del self.toolbar


class MerginProjectItem(QgsDataItem):
    """Data item to represent a Mergin project."""

    def __init__(self, parent, project, mc, project_manager):
        self.project = project
        self.project_name = posixpath.join(
            project["namespace"], project["name"]
        )  # we need posix path for server API calls
        QgsDataItem.__init__(self, QgsDataItem.Collection, parent, self.project_name, "/Mergin/" + self.project_name)
        self.path = mergin_project_local_path(self.project_name)
        # check local project dir was not unintentionally removed
        if self.path:
            if not os.path.exists(self.path):
                self.path = None
        if self.path:
            self.setIcon(QIcon(icon_path("folder-solid.svg")))
        else:
            self.setIcon(QIcon(icon_path("cloud-solid.svg")))
        self.mc = mc
        self.mpm = project_manager

    def download(self):
        settings = QSettings()

        last_parent_dir = settings.value("Mergin/lastUsedDownloadDir", "")

        parent_dir = QFileDialog.getExistingDirectory(None, "Open Directory", last_parent_dir, QFileDialog.ShowDirsOnly)
        if not parent_dir:
            return

        settings.setValue("Mergin/lastUsedDownloadDir", parent_dir)

        target_dir = os.path.abspath(os.path.join(parent_dir, self.project["name"]))

        if os.path.exists(target_dir):
            QMessageBox.warning(
                None,
                "Download Project",
                "The target directory already exists:\n" + target_dir + "\n\nPlease select a different directory.",
            )
            return

        dlg = SyncDialog()
        dlg.download_start(self.mc, target_dir, self.project_name)

        dlg.exec_()  # blocks until completion / failure / cancellation

        if dlg.exception:
            if isinstance(dlg.exception, (URLError, ValueError)):
                QgsApplication.messageLog().logMessage("Mergin plugin: " + str(dlg.exception))
                msg = (
                    "Failed to download your project {}.\n"
                    "Please make sure your Mergin settings are correct".format(self.project_name)
                )
                QMessageBox.critical(None, "Project download", msg, QMessageBox.Close)
            elif isinstance(dlg.exception, LoginError):
                login_error_message(dlg.exception)
            else:
                unhandled_exception_message(
                    dlg.exception_details(),
                    "Project download",
                    f"Failed to download project {self.project_name} due to an unhandled exception.",
                )
            return

        if not dlg.is_complete:
            return  # either it has been cancelled or an error has been thrown

        settings.setValue("Mergin/localProjects/{}/path".format(self.project_name), target_dir)
        self.path = target_dir
        self.setIcon(QIcon(icon_path("folder-solid.svg")))
        QApplication.restoreOverrideCursor()

        msg = "Your project {} has been successfully downloaded. " "Do you want to open project file?".format(
            self.project_name
        )
        btn_reply = QMessageBox.question(
            None, "Project download", msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )
        if btn_reply == QMessageBox.Yes:
            self.open_project()

    def remove_local_project(self):
        if not self.path:
            return
        cur_proj = QgsProject.instance()
        cur_proj_path = cur_proj.absolutePath()
        msg = (
            "Your local changes will be lost. Make sure your project is synchronised with server. \n\n"
            "Do you want to proceed?".format(self.project_name)
        )
        btn_reply = QMessageBox.question(
            None, "Remove local project", msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )
        if btn_reply == QMessageBox.No:
            return

        if os.path.exists(self.path):
            try:
                if same_dir(cur_proj_path, self.path):
                    msg = (
                        "The project is currently open. It will get cleared if you proceed.\n\n"
                        "Proceed anyway?".format(self.project_name)
                    )
                    btn_reply = QMessageBox.question(
                        None, "Remove local project", msg, QMessageBox.No | QMessageBox.No, QMessageBox.Yes
                    )
                    if btn_reply == QMessageBox.No:
                        return

                    cur_proj.clear()
                    registry = QgsProviderRegistry.instance()
                    registry.setLibraryDirectory(registry.libraryDirectory())

                # remove logging file handler
                mp = MerginProject(self.path)
                log_file_handler = mp.log.handlers[0]
                log_file_handler.close()
                mp.log.removeHandler(log_file_handler)
                del mp

                shutil.rmtree(self.path)
            except PermissionError as e:
                QgsApplication.messageLog().logMessage(f"Mergin plugin: {str(e)}")
                msg = (
                    f"Failed to delete your project {self.project_name} because it is open.\n"
                    "You might need to close project or QGIS to remove its files."
                )
                QMessageBox.critical(None, "Project delete", msg, QMessageBox.Close)
                return

        settings = QSettings()
        settings.remove("Mergin/localProjects/{}/path".format(self.project_name))
        self.path = None
        self.setIcon(QIcon(icon_path("cloud-solid.svg")))
        root_item = self.parent().parent()
        root_item.local_project_removed.emit()

    def _unsaved_changes_check(self):
        """Check if current project is same as actually operated mergin project
        and if there are some unsaved changes.
        :return: true if previous method should continue, false otherwise
        :type: boolean
        """
        qgis_files = find_qgis_files(self.path)
        if QgsProject.instance().fileName() in qgis_files:
            return True if unsaved_project_check() else False
        return True

    def _have_writing_permissions(self):
        """Check if user have writing rights to the project."""
        info = self.mc.project_info(self.project_name)
        username = self.mc.username()
        writersnames = info["access"]["writersnames"]
        return username in writersnames

    def open_project(self):
        if not self.path:
            return

        qgis_files = find_qgis_files(self.path)
        if len(qgis_files) == 1:
            iface.addProject(qgis_files[0])
        else:
            msg = (
                "Selected project does not contain any QGIS project file"
                if len(qgis_files) == 0
                else "Plugin can only load project with single QGIS project file but {} found.".format(len(qgis_files))
            )
            QMessageBox.warning(None, "Load QGIS project", msg, QMessageBox.Close)

    def project_status(self):
        if not self.path:
            return
        if not self._unsaved_changes_check():
            return
        self.mpm.project_status(self.path)

    def sync_project(self):
        if not self.path:
            return
        self.mpm.sync_project(self.path, self.project_name)

    def _reload_project(self):
        """ This will forcefully reload the QGIS project because the project (or its data) may have changed """
        qgis_files = find_qgis_files(self.path)
        if QgsProject.instance().fileName() in qgis_files:
            iface.addProject(QgsProject.instance().fileName())

    def clone_remote_project(self):
        user_info = self.mc.user_info()
        dlg = CloneProjectDialog(username=user_info["username"], user_organisations=user_info.get("organisations", []))
        if not dlg.exec_():
            return  # cancelled
        try:
            self.mc.clone_project(self.project_name, dlg.project_name, dlg.project_namespace)
            msg = "Mergin project cloned successfully."
            QMessageBox.information(None, "Clone project", msg, QMessageBox.Close)

            root_item = self.parent().parent()
            root_item.depopulate()
            # this would crash QGIS
            # groups = root_item.children()
            # for g in groups:
            #     g.refresh()
        except (URLError, ClientError) as e:
            msg = "Failed to clone project {}:\n\n{}".format(self.project_name, str(e))
            QMessageBox.critical(None, "Clone project", msg, QMessageBox.Close)
        except LoginError as e:
            login_error_message(e)

    def remove_remote_project(self):
        msg = "Do you really want to remove project {} from server?".format(self.project_name)
        btn_reply = QMessageBox.question(None, "Remove project", msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if btn_reply == QMessageBox.No:
            return

        try:
            self.mc.delete_project(self.project_name)
            msg = "Mergin project removed successfully."
            QMessageBox.information(None, "Remove project", msg, QMessageBox.Close)
            root_item = self.parent().parent()
            root_item.depopulate()
            # this would crash QGIS
            # groups = root_item.children()
            # for g in groups:
            #     g.refresh()
        except (URLError, ClientError) as e:
            msg = "Failed to remove project {}:\n\n{}".format(self.project_name, str(e))
            QMessageBox.critical(None, "Remove project", msg, QMessageBox.Close)
        except LoginError as e:
            login_error_message(e)

    def submit_logs(self):
        if not self.path:
            return

        logs_path = os.path.join(self.path, ".mergin", "client-log.txt")
        msg = (
            "This action will send a diagnostic log to the developers. "
            "Use this option when you encounter synchronization issues, as the log is "
            "very useful to determine the exact cause of the problem.\n\n"
            "The log does not contain any of your data, only file names. It can be found here:\n"
            "{}\n\nIt would be useful if you also send a mail to info@lutraconsulting.co.uk "
            "and briefly describe the problem to add more context to the diagnostic log.\n\n"
            "Please click OK if you want to proceed.".format(logs_path)
        )

        btn_reply = QMessageBox.question(None, "Submit diagnostic logs", msg, QMessageBox.Ok | QMessageBox.Cancel)
        if btn_reply != QMessageBox.Ok:
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        log_file_name, error = send_logs(self.mc.username(), logs_path)
        QApplication.restoreOverrideCursor()

        if error:
            QMessageBox.warning(
                None, "Submit diagnostic logs", "Sending of diagnostic logs failed!\n\n{}".format(error)
            )
            return
        QMessageBox.information(
            None,
            "Submit diagnostic logs",
            "Diagnostic logs successfully submitted - thank you!\n\n{}".format(log_file_name),
            QMessageBox.Close,
        )

    def actions(self, parent):
        action_download = QAction(QIcon(icon_path("cloud-download-alt-solid.svg")), "Download", parent)
        action_download.triggered.connect(self.download)

        action_remove_local = QAction(QIcon(icon_path("trash-solid.svg")), "Remove locally", parent)
        action_remove_local.triggered.connect(self.remove_local_project)

        action_open_project = QAction("Open QGIS project", parent)
        action_open_project.triggered.connect(self.open_project)

        action_sync_project = QAction(QIcon(icon_path("sync-solid.svg")), "Synchronize", parent)
        action_sync_project.triggered.connect(self.sync_project)

        action_clone_remote = QAction(QIcon(icon_path("copy-solid.svg")), "Clone", parent)
        action_clone_remote.triggered.connect(self.clone_remote_project)

        action_remove_remote = QAction(
            QIcon(icon_path("trash-alt-solid.svg")), "Remove from server", parent
        )
        action_remove_remote.triggered.connect(self.remove_remote_project)

        action_status = QAction(QIcon(icon_path("info-circle-solid.svg")), "Status", parent)
        action_status.triggered.connect(self.project_status)

        action_diagnostic_log = QAction(QIcon(icon_path("medkit-solid.svg")), "Diagnostic log", parent)
        action_diagnostic_log.triggered.connect(self.submit_logs)

        if self.path:
            actions = [
                action_open_project,
                action_status,
                action_sync_project,
                action_clone_remote,
                action_remove_local,
                action_diagnostic_log,
            ]
        else:
            actions = [action_download, action_clone_remote]
            if self.project["permissions"]["delete"]:
                actions.append(action_remove_remote)
        return actions


class MerginGroupItem(QgsDataCollectionItem):
    """ Mergin group data item. Contains filtered list of Mergin projects. """

    def __init__(self, parent, grp_name, grp_filter, icon, order, project_manager):
        QgsDataCollectionItem.__init__(self, parent, grp_name, "/Mergin" + grp_name)
        self.filter = grp_filter
        self.setIcon(QIcon(icon_path(icon)))
        self.setSortKey(order)
        self.project_manager = project_manager

    def createChildren(self):
        mc = self.parent().mc
        if not mc:
            error_item = QgsErrorItem(self, "Failed to login please check the configuration", "/Mergin/error")
            sip.transferto(error_item, self)
            return [error_item]
        try:
            projects = mc.projects_list(flag=self.filter)
        except URLError:
            error_item = QgsErrorItem(self, "Failed to get projects from server", "/Mergin/error")
            sip.transferto(error_item, self)
            return [error_item]
        except Exception as err:
            error_item = QgsErrorItem(self, "Error: {}".format(str(err)), "/Mergin/error")
            sip.transferto(error_item, self)
            return [error_item]

        items = []
        for project in projects:
            item = MerginProjectItem(self, project, mc, self.project_manager)
            item.setState(QgsDataItem.Populated)  # make it non-expandable
            sip.transferto(item, self)
            items.append(item)
        return items

    def actions(self, parent):
        action_refresh = QAction(QIcon(icon_path("redo-solid.svg")), "Reload", parent)
        action_refresh.triggered.connect(self.depopulate)
        actions = [action_refresh]
        if self.name() == "My projects":
            action_create = QAction(
                QIcon(icon_path("add.svg", fa_icon=False)), "Create new project", parent
            )
            action_create.triggered.connect(self.parent().show_create_project_dialog)
            actions.append(action_create)
        return actions


class MerginRootItem(QgsDataCollectionItem):
    """ Mergin root data containing project groups item with configuration dialog. """

    config_changed = pyqtSignal()
    local_project_removed = pyqtSignal()

    def __init__(self):
        QgsDataCollectionItem.__init__(self, None, "Mergin", "/Mergin")
        self.setIcon(QIcon(os.path.join(os.path.dirname(os.path.realpath(__file__)), "images/icon.png")))
        self.mc = None
        self.error = ""
        self.project_manager = None
        self.wizard = None
        try:
            self.mc = create_mergin_client()
            self.project_manager = MerginProjectsManager(self.mc)
        except (URLError, ClientError):
            self.error = "Plugin not configured or \n QGIS master password not set up"
        except Exception as err:
            self.error = "Error: {}".format(str(err))

    def createChildren(self):
        if not self.mc and self.error:
            error_item = QgsErrorItem(self, self.error, "/Mergin/error")
            error_item.setIcon(QIcon(icon_path("exclamation-triangle-solid.svg")))
            sip.transferto(error_item, self)
            return [error_item]

        items = []
        my_projects = MerginGroupItem(self, "My projects", "created", "user-solid.svg", 1, self.project_manager)
        my_projects.setState(QgsDataItem.Populated)
        my_projects.refresh()
        sip.transferto(my_projects, self)
        items.append(my_projects)

        shared_projects = MerginGroupItem(self, "Shared with me", "shared", "user-friends-solid.svg", 2, self.project_manager)
        shared_projects.setState(QgsDataItem.Populated)
        shared_projects.refresh()
        sip.transferto(shared_projects, self)
        items.append(shared_projects)

        all_projects = MerginGroupItem(self, "Explore", None, "list-solid.svg", 3, self.project_manager)
        all_projects.setState(QgsDataItem.Populated)
        all_projects.refresh()
        sip.transferto(all_projects, self)
        items.append(all_projects)

        return items

    def configure(self):
        dlg = ConfigurationDialog()
        if dlg.exec_():
            self.mc = dlg.writeSettings()
            self.depopulate()
            self.config_changed.emit()

    def show_create_project_dialog(self):
        if not unsaved_project_check():
            return
        if not self.project_manager:
            QMessageBox.warning(None, "Mergin Plugin Config Error",
                                "Mergin plugin configuration is invalid - project manager couldn't be created.")
            return
        user_info = self.mc.user_info()
        self.wizard = NewMerginProjectWizard(
            self.project_manager,
            username=user_info["username"],
            user_organisations=user_info.get("organisations", [])
        )
        if not self.wizard.exec_():
            self.wizard = None
            return  # cancelled
        self.depopulate()  # make sure the item has the link between remote and local project we have just added
        self.wizard = None

    def actions(self, parent):
        action_configure = QAction(QIcon(icon_path("cog-solid.svg")), "Configure", parent)
        action_configure.triggered.connect(self.configure)

        action_create = QAction(QIcon(icon_path("plus-square-solid.svg")), "Create new project", parent)
        action_create.triggered.connect(self.show_create_project_dialog)
        actions = [action_configure]
        if self.mc:
            actions.append(action_create)
        return actions


class DataItemProvider(QgsDataItemProvider):
    def __init__(self):
        QgsDataItemProvider.__init__(self)
        self.root_item = None

    def name(self):
        return "MerginProvider"

    def capabilities(self):
        return QgsDataProvider.Net

    def createDataItem(self, path, parentItem):
        if not parentItem:
            ri = MerginRootItem()
            sip.transferto(ri, None)
            self.root_item = ri
            return ri
        else:
            return None

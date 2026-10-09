// M7 / action 2b. Untrusted release content is always PlainText.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15
import ".."

Rectangle {
    id: root
    property string panelState: "available"
    property string version: ""
    property var notes: []
    property string currentVersion: ""
    property string checkedText: ""
    property bool releasePageAvailable: false
    property bool canCheckNow: false
    property bool autoCheck: false
    property string artifactTrack: ""
    property string artifactSizeText: ""
    property string downloadPhase: "idle"
    property int downloadPercent: 0
    property double downloadReceived: 0
    property double downloadTotal: 0
    property bool downloadBusy: false
    property bool downloadCancelling: false
    property bool canDownload: false
    property bool canRetryDownload: false
    property bool canCancelDownload: false
    property bool canOpenFolder: false
    property bool canInstallAndRestart: false
    property bool canSkipVersion: false
    property bool canRemindLater: false
    property string downloadError: ""
    property string downloadErrorText: ""
    property string downloadRetryText: ""
    property string folderPath: ""
    property string adminInstruction: ""
    property string installRefusal: ""
    property string installRefusalText: ""
    property string networkRefusal: ""
    signal releasePageRequested()
    signal skipRequested()
    signal remindLaterRequested()
    signal checkRequested()
    signal showSkippedRequested()
    signal downloadRequested()
    signal cancelRequested()
    signal retryRequested()
    signal openFolderRequested()
    signal installRequested()

    readonly property bool operationVisible: panelState === "available" && downloadPhase !== "idle"
        || panelState === "available"
    readonly property bool deb: artifactTrack === "deb" || downloadPhase === "readydeb"
    readonly property bool ready: downloadPhase === "readydeb" || downloadPhase === "readyappimage"
    readonly property bool failed: downloadPhase === "error" || panelState === "error-net"
    readonly property bool installing: downloadPhase === "installing"
    readonly property bool accent: operationVisible && !failed && networkRefusal === ""
    readonly property string downloadLabel: deb ? qsTr("Скачать пакет") : qsTr("Скачать новую версию")
    readonly property bool folderAction: downloadPhase === "readydeb"
        || (downloadPhase === "readyappimage" && installRefusal === "unsupported")
        || (failed && !canInstallAndRestart && !canRetryDownload && !canDownload && canOpenFolder)
    readonly property bool installAction: downloadPhase === "readyappimage" && !folderAction
        || installing || (failed && canInstallAndRestart)
    readonly property string primaryText: folderAction ? qsTr("Открыть папку")
        : installAction ? (failed ? qsTr("Повторить установку") : qsTr("Установить и перезапустить"))
        : failed && canRetryDownload ? qsTr("Повторить скачивание") : downloadLabel
    readonly property bool primaryEnabled: !downloadBusy && !installing
        && (folderAction ? canOpenFolder : installAction ? canInstallAndRestart
            : failed ? canRetryDownload || canDownload : canDownload)
    readonly property string operationTitle: {
        if (networkRefusal === "offline" && downloadPhase === "idle") return qsTr("Скачивание недоступно");
        switch (downloadPhase) {
        case "metadata": return qsTr("Проверяю сведения перед скачиванием…");
        case "downloading": return qsTr("Скачиваю обновление…");
        case "verifying": return qsTr("Проверяю подпись и целостность…");
        case "readydeb": return qsTr("Пакет готов. Подпись и целостность проверены.");
        case "readyappimage": return qsTr("Новая версия готова. Подпись и целостность проверены.");
        case "cancelled": return qsTr("Скачивание отменено. Обновление не установлено.");
        case "installing": return qsTr("Устанавливаю новую версию…");
        case "error":
            if (["signature-invalid", "hash-mismatch", "size-mismatch", "metadata-invalid", "metadata-too-large", "release-mismatch"].indexOf(downloadError) >= 0)
                return qsTr("Не удалось проверить обновление");
            if (downloadError === "no-space") return qsTr("Не хватает места для обновления");
            if (["write-failed", "path-unsafe"].indexOf(downloadError) >= 0)
                return qsTr("Не удалось сохранить обновление");
            if (["file-changed"].indexOf(downloadError) >= 0)
                return qsTr("Проверенный файл больше недоступен");
            if (["install-refused", "install-failed", "prepare-failed", "shutdown-incomplete", "launch-failed", "launch-unconfirmed"].indexOf(downloadError) >= 0)
                return qsTr("Не удалось установить обновление");
            return qsTr("Не удалось скачать обновление");
        default: return qsTr("Скачивание начнётся по вашему нажатию.");
        }
    }
    readonly property string restartNotice: qsTr("Установка заменит текущую копию программы в домашней папке. Модели и настройки сохранятся. Программа закроется для запуска новой версии. Если она не откроется, запустите Astra Voice из меню.")
    readonly property string installingNotice: qsTr("Программа закроется для запуска новой версии. Если она не откроется, запустите Astra Voice из меню. Ошибка после закрытия будет показана при следующем запуске.")
    readonly property string operationBody: {
        if (downloadCancelling) return qsTr("Отменяю скачивание…");
        if (failed) return downloadErrorText + (downloadRetryText ? "\n" + downloadRetryText : "");
        if (installRefusalText && (ready || installAction)) return installRefusal === "busy"
            ? qsTr("Завершите диктовку, чтобы установить обновление")
            : installRefusal === "unsupported" ? qsTr("Эту копию нельзя обновить автоматически. Откройте папку с проверенным файлом и запустите его вручную")
            : installRefusalText;
        switch (downloadPhase) {
        case "metadata": return qsTr("Размер подтверждён подписью. Повторно проверяю сведения о файле.");
        case "verifying": return qsTr("Файл скачан. Пока проверка не закончена, использовать его нельзя.");
        case "readydeb": return qsTr("Установку выполняет администратор. Передайте ему пакет и файлы проверки из этой папки.");
        case "readyappimage": return restartNotice;
        case "installing": return installingNotice;
        case "cancelled": return qsTr("Можно скачать эту версию ещё раз.");
        case "downloading": return "";
        default:
            if (!canDownload) return networkRefusal === "offline"
                ? qsTr("Чтобы скачать обновление, выключите офлайн-режим в настройках ниже.")
                : networkRefusal ? qsTr("Ограничение задано администратором.")
                : qsTr("Для этой сборки файл обновления недоступен");
            return deb ? qsTr("Установку выполняет администратор.")
                : qsTr("Установка и перезапуск — отдельным нажатием после проверки.");
        }
    }
    function focusHeading() { heading.forceActiveFocus(Qt.TabFocusReason); return heading; }
    function megabytes(value) { return (value / 1000000).toFixed(value < 10000000 ? 1 : 0) + " МБ"; }
    function ownsFocus() {
        var item = root.Window.window ? root.Window.window.activeFocusItem : null;
        while (item) { if (item === root) return true; item = item.parent; }
        return false;
    }
    onDownloadPhaseChanged: {
        var hadCancelFocus = cancelButton.activeFocus;
        var hadPanelFocus = root.ownsFocus();
        Qt.callLater(function() {
            if (downloadPhase === "cancelled" && hadCancelFocus) primaryButton.forceActiveFocus(Qt.TabFocusReason);
            else if ((downloadPhase === "error" && hadPanelFocus) || (hadCancelFocus && !downloadBusy)) root.focusHeading();
            else if (downloadBusy && primaryButton.activeFocus && canCancelDownload)
                cancelButton.forceActiveFocus(Qt.TabFocusReason);
        });
    }
    Keys.onEscapePressed: {
        if (root.downloadBusy && root.canCancelDownload) { root.cancelRequested(); event.accepted = true; }
        else event.accepted = false;
    }
    implicitHeight: body.implicitHeight + (Theme.updatePanelPaddingY + Theme.cardBorder) * 2
    height: implicitHeight
    radius: Theme.updatePanelRadius
    color: Theme.bgSurface
    border.width: Theme.cardBorder
    border.color: failed ? Theme.dangerInk : accent ? Theme.primary : Theme.border

    component SmallText: Text {
        textFormat: Text.PlainText
        color: Theme.fgMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontCaptionSize
        lineHeight: Math.round(Theme.fontCaptionSize * 1.4)
        lineHeightMode: Text.FixedHeight
        renderType: Text.NativeRendering
        wrapMode: Text.Wrap
    }
    component Selectable: TextEdit {
        textFormat: TextEdit.PlainText
        readOnly: true
        selectByMouse: true
        activeFocusOnTab: false
        color: Theme.fgSecondary
        font.family: Theme.fontMono
        font.pixelSize: 12
        wrapMode: TextEdit.Wrap
        renderType: Text.NativeRendering
    }
    Rectangle {
        x: body.x + heading.x - Theme.focusOffset - Theme.focusWidth
        y: body.y + heading.y - Theme.focusOffset - Theme.focusWidth
        width: heading.width + 2 * (Theme.focusOffset + Theme.focusWidth)
        height: heading.height + 2 * (Theme.focusOffset + Theme.focusWidth)
        radius: Theme.focusRadius
        color: "transparent"
        border.width: Theme.focusWidth
        border.color: Theme.stateFocusRing
        visible: heading.activeFocus
    }
    Column {
        id: body
        x: Theme.updatePanelPaddingX + Theme.cardBorder
        y: Theme.updatePanelPaddingY + Theme.cardBorder
        width: root.width - x * 2
        NoteBanner {
            width: parent.width
            visible: root.networkRefusal !== ""
            height: visible ? implicitHeight : 0
            title: root.networkRefusal === "offline" ? qsTr("Включён офлайн-режим") : qsTr("Обновления ограничены администратором")
            body: root.networkRefusal === "offline" ? qsTr("Проверка и скачивание обновлений недоступны. Диктовка работает без сети.") : qsTr("Сетевые действия ограничены: задано администратором.")
        }
        Item { height: 12; width: 1; visible: root.networkRefusal !== "" }
        Flow {
            id: heading
            objectName: "updatePanelHeading"
            width: parent.width
            height: Math.max(implicitHeight, Math.ceil(Theme.badgeHeight))
            spacing: 9
            activeFocusOnTab: false
            Accessible.role: Accessible.Heading
            Accessible.name: title.text + " " + releaseVersion.text
            KeyNavigation.tab: primaryButton.visible && primaryButton.enabled ? primaryButton : releaseButton
            SmallText {
                id: title
                objectName: "updatePanelTitle"
                width: Math.min(implicitWidth, heading.width)
                font.pixelSize: Theme.fontH3SubsectionSize
                lineHeight: Math.round(Theme.fontH3SubsectionSize * Theme.fontH3SubsectionLineHeight)
                font.weight: Font.Medium
                color: Theme.fg
                text: root.panelState === "uptodate" ? qsTr("Установлена последняя версия")
                    : root.panelState === "checking" ? qsTr("Проверяю обновления…")
                    : root.panelState === "skipped" ? qsTr("Версия")
                    : root.panelState === "unavailable" ? qsTr("Источник обновлений недоступен")
                    : root.panelState === "error-net" ? qsTr("Не удалось проверить сведения об обновлении")
                    : qsTr("Доступна версия")
            }
            Selectable {
                id: releaseVersion
                objectName: "updateReleaseVersion"
                width: Math.min(implicitWidth, heading.width)
                font.pixelSize: Theme.fontH3SubsectionSize
                text: root.version
                visible: root.panelState === "available" || root.panelState === "skipped"
            }
            SmallText { text: qsTr("пропущена"); visible: root.panelState === "skipped"; font.pixelSize: 16; color: Theme.fg }
            Rectangle {
                objectName: "updateNewBadge"
                visible: root.panelState === "available" && root.downloadPhase === "idle" && root.networkRefusal === ""
                width: badgeText.implicitWidth + Theme.badgePaddingX * 2
                height: Math.ceil(Theme.badgeHeight)
                radius: Theme.badgeRadius
                color: Theme.primaryBg
                Text { id: badgeText; anchors.centerIn: parent; text: qsTr("Новая"); textFormat: Text.PlainText; color: Theme.primary; font.family: Theme.fontUi; font.pixelSize: Theme.badgeSize }
            }
        }
        SmallText {
            objectName: "updateArtifactMetadata"
            width: parent.width
            text: root.panelState === "checking" ? qsTr("Ищу новую версию и проверяю подпись сведений о ней.")
                : root.panelState === "skipped" ? qsTr("Напомним о следующей версии. Обновиться до этой всё ещё можно.")
                : root.operationVisible && root.artifactSizeText ? (root.deb ? ".deb" : "AppImage") + " · " + root.artifactSizeText + qsTr(" · размер из проверенных сведений") + (root.networkRefusal === "offline" ? qsTr(" · сохранённые сведения о версии") : "") : root.checkedText
            visible: text !== ""
            topPadding: 6
        }
        Flow {
            objectName: "updateInstalledLine"
            width: parent.width
            visible: root.panelState === "uptodate"
            SmallText { text: qsTr("Версия") + " "; visible: root.currentVersion !== "" }
            SmallText { objectName: "updateInstalledVersion"; text: root.currentVersion; font.family: Theme.fontMono }
            SmallText {
                width: Math.min(implicitWidth, body.width)
                text: ". " + (root.autoCheck ? qsTr("Следующая автоматическая проверка — не раньше чем через сутки.") : qsTr("Автоматическая проверка выключена — проверяйте вручную, когда удобно."))
            }
        }
        Item { width: 1; height: 7; visible: root.operationVisible }
        SmallText { width: parent.width; text: qsTr("Что нового"); color: Theme.fgSecondary; font.pixelSize: 13; font.weight: Font.Bold; visible: root.operationVisible }
        Item { width: 1; height: 6; visible: root.operationVisible }
        Column {
            id: notesList
            objectName: "updateNotes"
            width: parent.width
            visible: root.operationVisible
            FontMetrics { id: capBox; font.family: Theme.fontUi; font.pixelSize: Theme.updatePanelListSize }
            Repeater {
                model: root.notes
                Item {
                    required property var modelData
                    width: notesList.width
                    height: noteLine.implicitHeight
                    Rectangle {
                        objectName: "updateNoteMarker"
                        visible: parent.modelData.bullet === true
                        x: 2
                        y: Math.round(noteLine.baselineOffset + capBox.tightBoundingRect("Н").y + 4.5 - height / 2)
                        width: 4; height: 4; radius: 2; color: Theme.fgSecondary
                    }
                    Text {
                        id: noteLine
                        objectName: "updateNoteLine"
                        x: 17; width: parent.width - x
                        textFormat: Text.PlainText
                        text: String(parent.modelData.text)
                        color: Theme.fgSecondary
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.updatePanelListSize
                        lineHeight: Math.round(Theme.updatePanelListSize * Theme.updatePanelListLineHeight)
                        lineHeightMode: Text.FixedHeight
                        renderType: Text.NativeRendering
                        wrapMode: Text.Wrap
                    }
                }
            }
            SmallText { width: parent.width; text: qsTr("Описание изменений не предоставлено"); visible: root.notes.length === 0 }
        }
        Item { width: 1; height: 12; visible: operation.visible }
        Rectangle {
            id: operation
            objectName: "updateOperation"
            width: parent.width
            height: Math.max(root.deb ? 190 : Math.max(120, Math.max(reservedReadyText.implicitHeight, reservedInstallingText.implicitHeight) + 78), operationContent.implicitHeight + 20)
            radius: 7
            color: root.failed ? Theme.dangerBg : root.ready ? Theme.successBg : Theme.bgSurface2
            visible: root.operationVisible
            // Reserve the ready/restart explanation before downloading: actions stay fixed.
            SmallText { id: reservedReadyText; width: parent.width - 24; text: root.restartNotice; visible: false; Accessible.ignored: true }
            SmallText { id: reservedInstallingText; width: parent.width - 24; text: root.installingNotice; visible: false; Accessible.ignored: true }
            Column {
                id: operationContent
                x: 12; y: 10; width: parent.width - 24
                SmallText {
                    objectName: "updateOperationTitle"
                    width: parent.width
                    text: root.operationTitle
                    font.pixelSize: 13
                    lineHeight: Math.round(13 * 1.45)
                    font.weight: Font.Medium
                    color: root.failed ? Theme.dangerInk : root.ready ? Theme.successInk : Theme.fgSecondary
                    Accessible.name: text
                    Accessible.role: root.failed ? Accessible.AlertMessage : Accessible.StaticText
                }
                Item { width: 1; height: 4 }
                SmallText { objectName: "updateOperationBody"; width: parent.width; text: root.operationBody }
                Item { width: 1; height: 7; visible: path.visible }
                Selectable { id: path; objectName: "updateFolderPath"; width: parent.width; text: root.folderPath; visible: root.canOpenFolder && root.folderPath !== "" && (root.deb || root.folderAction) }
                Item { width: 1; height: 7; visible: instruction.visible }
                SmallText { id: instruction; width: parent.width; text: qsTr("Администратору: в этой папке выполните"); color: Theme.fgSecondary; visible: root.downloadPhase === "readydeb" && root.adminInstruction !== "" }
                Selectable { objectName: "updateAdminInstruction"; width: parent.width; text: root.adminInstruction; visible: instruction.visible }
                Item { width: 1; height: 6 }
                Item {
                    width: parent.width; height: 17
                    SmallText { objectName: "updateDownloadCounter"; width: parent.width; visible: root.downloadPhase === "downloading"; text: root.downloadPercent + " % · " + root.megabytes(root.downloadReceived) + qsTr(" из ") + root.megabytes(root.downloadTotal) }
                }
                Item { width: 1; height: 6 }
                Rectangle {
                    objectName: "updateProgress"
                    width: parent.width; height: 6; radius: 3
                    color: root.downloadBusy || root.installing ? Theme.border : "transparent"
                    Accessible.ignored: !root.downloadBusy && !root.installing
                    Accessible.role: Accessible.ProgressBar
                    Accessible.name: root.operationTitle + (root.downloadPhase === "downloading" ? " " + root.downloadPercent + " %" : "")
                    Rectangle { height: parent.height; radius: 3; color: Theme.primary; visible: root.downloadBusy || root.installing; x: root.downloadPhase === "downloading" ? 0 : parent.width * 0.35; width: parent.width * (root.downloadPhase === "downloading" ? Math.max(0, Math.min(100, root.downloadPercent)) / 100 : 0.3) }
                }
            }
        }
        Item { width: 1; height: 12; visible: actions.visible }
        Flow {
            id: actions
            objectName: "updateActionRow"
            width: parent.width
            spacing: 8
            visible: root.operationVisible
            AvButton {
                id: primaryButton
                objectName: "updatePrimary"
                width: body.width < 364 ? Math.min(body.width, Math.max(264, implicitWidth)) : Math.max(264, implicitWidth)
                text: root.primaryText
                variant: "primary"
                enabled: root.primaryEnabled
                Keys.onPressed: { if (event.isAutoRepeat) event.accepted = true; }
                onClicked: {
                    if (root.folderAction) root.openFolderRequested();
                    else if (root.installAction) root.installRequested();
                    else if (root.failed && root.canRetryDownload) root.retryRequested();
                    else root.downloadRequested();
                    Qt.callLater(function() { if (root.downloadBusy && root.canCancelDownload) cancelButton.forceActiveFocus(Qt.TabFocusReason); });
                }
            }
            Item {
                width: Math.max(92, cancelButton.implicitWidth)
                height: primaryButton.height
                AvButton { id: cancelButton; objectName: "updateCancel"; width: parent.width; text: qsTr("Отмена"); visible: root.downloadBusy; enabled: root.canCancelDownload; onClicked: root.cancelRequested() }
            }
            AvButton { id: releaseButton; objectName: "updateReleasePage"; visible: root.releasePageAvailable; enabled: root.networkRefusal === ""; text: qsTr("Страница выпуска"); iconName: "out"; onClicked: root.releasePageRequested() }
        }
        Item { width: 1; height: 8; visible: deferrals.visible }
        Flow {
            id: deferrals
            width: parent.width
            spacing: 8
            visible: root.operationVisible
            AvButton { objectName: "updateSkip"; text: qsTr("Пропустить эту версию"); enabled: root.canSkipVersion && !root.downloadBusy && !root.installing; onClicked: root.skipRequested() }
            AvButton { objectName: "updateRemindLater"; variant: "ghost"; text: qsTr("Напомнить позже"); enabled: root.canRemindLater && !root.downloadBusy && !root.installing; onClicked: root.remindLaterRequested() }
        }
        Flow {
            width: parent.width
            spacing: 8
            visible: !root.operationVisible && root.panelState !== "checking"
            AvButton { objectName: "updateCheckAgain"; visible: root.panelState === "uptodate" || root.panelState === "error-net" || root.panelState === "unavailable"; text: root.panelState === "error-net" ? qsTr("Повторить проверку") : qsTr("Проверить ещё раз"); enabled: root.canCheckNow; onClicked: root.checkRequested() }
            AvButton { objectName: "updateShowSkipped"; visible: root.panelState === "skipped"; text: qsTr("Показать %1").arg(root.version); onClicked: root.showSkippedRequested() }
        }
    }
}

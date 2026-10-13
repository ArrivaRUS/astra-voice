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
    property string downloadError: ""
    property string downloadErrorText: ""
    property string downloadRetryText: ""
    property string folderPath: ""
    property string adminInstruction: ""
    property string installRefusal: ""
    property string installRefusalText: ""
    property string networkRefusal: ""
    signal skipRequested()
    signal checkRequested()
    signal showSkippedRequested()
    signal downloadRequested()
    signal cancelRequested()
    signal retryRequested()
    signal openFolderRequested()
    signal installRequested()

    readonly property bool operationVisible: panelState === "available"
    readonly property bool operationBlockVisible: operationVisible
        && (downloadPhase !== "idle" || !canDownload || networkRefusal !== "")
    readonly property bool cancellablePhase: ["metadata", "downloading", "verifying"].indexOf(downloadPhase) >= 0
    readonly property bool progressVisible: cancellablePhase || installing
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
        if (downloadPhase === "idle") return qsTr("Скачивание недоступно");
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
        default: return "";
        }
    }
    readonly property string restartNotice: qsTr("Программа закроется и откроется уже новой версией. Модели и настройки сохранятся. Если она не откроется, запустите Astra Voice из меню.")
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
        case "downloading": return downloadPercent + " % · " + megabytes(downloadReceived) + qsTr(" из ") + megabytes(downloadTotal);
        default:
            if (networkRefusal) return networkRefusal === "offline"
                ? qsTr("Чтобы скачать обновление, выключите офлайн-режим в настройках ниже.")
                : qsTr("Ограничение задано администратором.");
            if (downloadRetryText) return downloadRetryText;
            if (!canDownload) return qsTr("Для этой сборки файл обновления недоступен");
            return "";
        }
    }
    // Measure every label in a track so changing the action never changes its slot.
    readonly property real primarySlotWidth: Math.ceil(Math.max(downloadMetrics.width,
        folderMetrics.width, retryDownloadMetrics.width,
        deb ? 0 : installMetrics.width, deb ? 0 : retryInstallMetrics.width)) + 2 * Theme.buttonPaddingX
    TextMetrics { id: downloadMetrics; font: primaryButton.font; text: root.downloadLabel }
    TextMetrics { id: folderMetrics; font: primaryButton.font; text: qsTr("Открыть папку") }
    TextMetrics { id: retryDownloadMetrics; font: primaryButton.font; text: qsTr("Повторить скачивание") }
    TextMetrics { id: installMetrics; font: primaryButton.font; text: qsTr("Установить и перезапустить") }
    TextMetrics { id: retryInstallMetrics; font: primaryButton.font; text: qsTr("Повторить установку") }
    TextMetrics { id: primaryTextMetrics; font: primaryButton.font; text: root.primaryText }
    property bool headingKeyboardFocus: false
    property bool pendingCancelFocus: false
    property bool pendingCancelKeyboard: false
    function panelActions() {
        return [primaryButton, skipButton, cancelButton,
            checkAgainButton, showSkippedButton];
    }
    function focusPanelAction(current, direction) {
        var actions = panelActions();
        var start = current ? actions.indexOf(current) + direction : 0;
        if (current && actions.indexOf(current) < 0) return false;
        for (var i = start; i >= 0 && i < actions.length; i += direction) {
            if (actions[i].visible && actions[i].enabled) {
                actions[i].forceActiveFocus(direction > 0 ? Qt.TabFocusReason : Qt.BacktabFocusReason);
                return true;
            }
        }
        // At the panel boundary Qt continues its normal focus chain into the section.
        return false;
    }
    function focusHeading(keyboard) {
        headingKeyboardFocus = keyboard === true;
        heading.forceActiveFocus(headingKeyboardFocus ? Qt.TabFocusReason : Qt.OtherFocusReason);
        return heading;
    }
    function megabytes(value) { return (value / 1000000).toFixed(value < 10000000 ? 1 : 0) + " МБ"; }
    function ownsFocus() {
        var item = root.Window.window ? root.Window.window.activeFocusItem : null;
        while (item) { if (item === root) return true; item = item.parent; }
        return false;
    }
    function keyboardFocus() {
        var item = root.Window.window ? root.Window.window.activeFocusItem : null;
        return item === heading ? headingKeyboardFocus
            : item && typeof item.visualFocus !== "undefined" ? item.visualFocus : false;
    }
    function focusCanReturn(source) {
        var window = root.Window.window;
        if (!window || !root.visible) return false;
        var item = window.activeFocusItem;
        // Qt moves focus from a disabled/hidden button to an enclosing scope.
        // A newly selected sibling or outside control must keep its focus.
        while (source) {
            if (source === item) return true;
            source = source.parent;
        }
        return item === null;
    }
    function requestCancel(keyboard) {
        // Cancellation can disable its button before downloadPhase changes.
        // Preserve its focus origin before the synchronous bridge mutation.
        pendingCancelFocus = cancelButton.activeFocus;
        pendingCancelKeyboard = keyboard === true;
        cancelRequested();
    }
    Connections {
        target: root.Window.window
        function onActiveFocusItemChanged() {
            var window = root.Window.window;
            var item = window ? window.activeFocusItem : null;
            // A disabled control falls back to an enclosing focus scope. A real
            // move to another control takes precedence over the pending return.
            if (!root.pendingCancelFocus || !item) return;
            var ancestor = cancelButton;
            while (ancestor) {
                if (ancestor === item) return;
                ancestor = ancestor.parent;
            }
            root.pendingCancelFocus = false;
        }
    }
    onVisibleChanged: { if (!visible) pendingCancelFocus = false; }
    onDownloadPhaseChanged: {
        var hadCancelFocus = cancelButton.activeFocus || pendingCancelFocus;
        var hadPanelFocus = root.ownsFocus();
        var focusSource = hadCancelFocus ? cancelButton
            : root.Window.window ? root.Window.window.activeFocusItem : null;
        var keyboard = pendingCancelFocus ? pendingCancelKeyboard
            : focusSource === primaryButton && primaryButton.requestInProgress
                ? primaryButton.activationKeyboard : root.keyboardFocus();
        if (!cancellablePhase) pendingCancelFocus = false;
        Qt.callLater(function() {
            if (!root.focusCanReturn(focusSource)) return;
            var reason = keyboard ? Qt.TabFocusReason : Qt.OtherFocusReason;
            if (downloadPhase === "cancelled" && hadCancelFocus) primaryButton.forceActiveFocus(reason);
            else if ((downloadPhase === "error" && hadPanelFocus) || (hadCancelFocus && !cancellablePhase)) root.focusHeading(keyboard);
            else if (downloadBusy && focusSource === primaryButton && canCancelDownload)
                cancelButton.forceActiveFocus(reason);
        });
    }
    Keys.onEscapePressed: {
        if (root.downloadBusy && root.canCancelDownload) { root.requestCancel(true); event.accepted = true; }
        else event.accepted = false;
    }
    implicitHeight: body.implicitHeight + (Theme.updatePanelPaddingY + Theme.cardBorder) * 2
    height: implicitHeight
    radius: Theme.updatePanelRadius
    color: Theme.bgSurface
    border.width: Theme.cardBorder
    border.color: failed ? Theme.dangerInk : accent ? Theme.primary : Theme.border

    component PanelButton: AvButton {
        id: control
        property bool enterArmed: false
        property int enterKey: 0
        property string enterPhase: ""
        property string enterPanelState: ""
        property string enterText: ""
        property bool activationKeyboard: false
        property bool spaceArmed: false
        onActiveFocusChanged: { if (!activeFocus) { enterArmed = false; spaceArmed = false; } }
        onEnabledChanged: { if (!enabled) { enterArmed = false; spaceArmed = false; } }
        onVisibleChanged: { if (!visible) { enterArmed = false; spaceArmed = false; } }
        onTextChanged: enterArmed = false
        // Pointer presses reset an earlier keyboard activation. Space is handled
        // by Button itself, after Keys has recorded its keyboard origin.
        onPressed: activationKeyboard = spaceArmed
        Accessible.onPressAction: {
            if (!enabled || !visible) return;
            activationKeyboard = true;
            if (!activeFocus) forceActiveFocus(Qt.TabFocusReason);
            clicked();
            activationKeyboard = false;
        }
        Keys.onPressed: {
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                event.accepted = true;
                if (!event.isAutoRepeat && !enterArmed && enabled && visible && activeFocus) {
                    enterArmed = true;
                    enterKey = event.key;
                    enterPhase = root.downloadPhase;
                    enterPanelState = root.panelState;
                    enterText = text;
                }
            } else if (event.key === Qt.Key_Space) {
                if (!event.isAutoRepeat && enabled && visible && activeFocus) {
                    spaceArmed = true;
                    activationKeyboard = true;
                }
                event.accepted = false;
            } else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
                enterArmed = false;
                var backward = event.key === Qt.Key_Backtab || (event.modifiers & Qt.ShiftModifier);
                event.accepted = root.focusPanelAction(control, backward ? -1 : 1);
            }
        }
        Keys.onReleased: {
            if (event.key === Qt.Key_Space) {
                event.accepted = false;
                Qt.callLater(function() { spaceArmed = false; activationKeyboard = false; });
                return;
            }
            if (event.key !== Qt.Key_Return && event.key !== Qt.Key_Enter) return;
            event.accepted = true;
            if (event.isAutoRepeat) return;
            var activate = enterArmed && enterKey === event.key && enabled && visible && activeFocus
                && enterPhase === root.downloadPhase && enterPanelState === root.panelState
                && enterText === text;
            enterArmed = false;
            if (activate) {
                activationKeyboard = true;
                // Qt 5.15 keeps the focus reason when the control already owns
                // focus. Pass the activation origin to the destination instead.
                clicked();
                activationKeyboard = false;
            }
        }
    }
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
        objectName: "updateHeadingFocusRing"
        x: body.x + heading.x + headingContent.x + headingContent.visibleChildrenRect.x - Theme.focusOffset - Theme.focusWidth
        y: body.y + heading.y + headingContent.y + headingContent.visibleChildrenRect.y - Theme.focusOffset - Theme.focusWidth
        width: headingContent.visibleChildrenRect.width + 2 * (Theme.focusOffset + Theme.focusWidth)
        height: headingContent.visibleChildrenRect.height + 2 * (Theme.focusOffset + Theme.focusWidth)
        radius: Theme.focusRadius
        color: "transparent"
        border.width: Theme.focusWidth
        border.color: Theme.stateFocusRing
        visible: heading.activeFocus && root.headingKeyboardFocus
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
        Item {
            id: heading
            objectName: "updatePanelHeading"
            width: parent.width
            height: Math.max(headingContent.implicitHeight, Math.ceil(Theme.badgeHeight))
            activeFocusOnTab: false
            Accessible.role: Accessible.Heading
            Accessible.name: title.text + " " + releaseVersion.text
            // One-way entry keeps programmatic heading focus out of the reverse Tab chain.
            Keys.onTabPressed: event.accepted = root.focusPanelAction(null, 1)
            Flow {
                id: headingContent
                objectName: "updateHeadingContent"
                width: parent.width
                spacing: 9
                // childrenRect also contains hidden items; their old Flow positions
                // must not enlarge the ring after the badge/version disappears.
                readonly property rect visibleChildrenRect: {
                    var bounds = childrenRect;
                    var left = bounds.x + bounds.width;
                    var top = bounds.y + bounds.height;
                    var right = bounds.x;
                    var bottom = bounds.y;
                    for (var i = 0; i < children.length; i++) {
                        var child = children[i];
                        if (!child.visible || child.width <= 0 || child.height <= 0) continue;
                        left = Math.min(left, child.x);
                        top = Math.min(top, child.y);
                        right = Math.max(right, child.x + child.width);
                        bottom = Math.max(bottom, child.y + child.height);
                    }
                    return Qt.rect(left, top, Math.max(0, right - left), Math.max(0, bottom - top));
                }
                SmallText {
                    id: title
                    objectName: "updatePanelTitle"
                    width: Math.min(implicitWidth, headingContent.width)
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
                    width: Math.min(implicitWidth, headingContent.width)
                    font.pixelSize: Theme.fontH3SubsectionSize
                    color: Theme.fg
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
                    Text { id: badgeText; anchors.centerIn: parent; text: qsTr("Новая"); textFormat: Text.PlainText; color: Theme.primary; font.family: Theme.fontUi; font.pixelSize: Theme.badgeSize; font.weight: Theme.badgeWeight }
                }
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
            height: visible ? Math.max(root.downloadPhase !== "idle" ? 77 : 0, operationContent.implicitHeight + 20) : 0
            radius: 7
            color: root.failed ? Theme.dangerBg : root.ready ? Theme.successBg : Theme.bgSurface2
            visible: root.operationBlockVisible
            Column {
                id: operationContent
                objectName: "updateOperationContent"
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
                SmallText { objectName: "updateOperationBody"; width: parent.width; text: root.operationBody; color: root.ready ? Theme.fgSecondary : Theme.fgMuted }
                Item { width: 1; height: 7; visible: path.visible }
                Selectable { id: path; objectName: "updateFolderPath"; width: parent.width; text: root.folderPath; visible: root.canOpenFolder && root.folderPath !== "" && (root.deb || root.folderAction) }
                Item { width: 1; height: 7; visible: instruction.visible }
                SmallText { id: instruction; width: parent.width; text: qsTr("Администратору: в этой папке выполните"); color: Theme.fgSecondary; visible: root.downloadPhase === "readydeb" && root.adminInstruction !== "" }
                Item { width: 1; height: 7; visible: instruction.visible }
                Selectable { objectName: "updateAdminInstruction"; width: parent.width; text: root.adminInstruction; visible: instruction.visible }
                Item { width: 1; height: 8; visible: root.progressVisible }
                Rectangle {
                    objectName: "updateProgress"
                    width: parent.width; height: 6; radius: 3
                    visible: root.progressVisible
                    color: Theme.border
                    Accessible.ignored: !visible
                    Accessible.role: Accessible.ProgressBar
                    Accessible.name: root.operationTitle + (root.downloadPhase === "downloading" ? " " + root.downloadPercent + " %" : "")
                    Rectangle { height: parent.height; radius: 3; color: Theme.primary; x: root.downloadPhase === "downloading" ? 0 : parent.width * 0.35; width: parent.width * (root.downloadPhase === "downloading" ? Math.max(0, Math.min(100, root.downloadPercent)) / 100 : 0.3) }
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
            PanelButton {
                id: primaryButton
                objectName: "updatePrimary"
                property bool requestInProgress: false
                width: root.primarySlotWidth
                implicitWidth: root.primarySlotWidth
                leftPadding: Math.max(Theme.buttonPaddingX, (width - primaryTextMetrics.width) / 2)
                rightPadding: leftPadding
                text: root.primaryText
                variant: "primary"
                enabled: root.primaryEnabled
                onClicked: {
                    var keyboard = primaryButton.activationKeyboard;
                    requestInProgress = true;
                    if (root.folderAction) root.openFolderRequested();
                    else if (root.installAction) root.installRequested();
                    else if (root.failed && root.canRetryDownload) root.retryRequested();
                    else root.downloadRequested();
                    requestInProgress = false;
                    Qt.callLater(function() {
                        if (root.cancellablePhase && root.canCancelDownload && root.focusCanReturn(primaryButton))
                            cancelButton.forceActiveFocus(keyboard ? Qt.TabFocusReason : Qt.OtherFocusReason);
                    });
                }
            }
            PanelButton { id: skipButton; objectName: "updateSkip"; text: qsTr("Пропустить эту версию"); enabled: root.canSkipVersion && !root.downloadBusy && !root.installing; onClicked: root.skipRequested() }
            PanelButton { id: cancelButton; objectName: "updateCancel"; text: qsTr("Отмена"); visible: root.cancellablePhase; enabled: root.canCancelDownload; onClicked: root.requestCancel(cancelButton.activationKeyboard) }
        }
        Flow {
            width: parent.width
            spacing: 8
            visible: !root.operationVisible && root.panelState !== "checking"
            PanelButton { id: checkAgainButton; objectName: "updateCheckAgain"; visible: root.panelState === "uptodate" || root.panelState === "error-net" || root.panelState === "unavailable"; text: root.panelState === "error-net" ? qsTr("Повторить проверку") : qsTr("Проверить ещё раз"); enabled: root.canCheckNow; onClicked: root.checkRequested() }
            PanelButton { id: showSkippedButton; objectName: "updateShowSkipped"; visible: root.panelState === "skipped"; text: qsTr("Показать %1").arg(root.version); onClicked: root.showSkippedRequested() }
        }
    }
}

// Окно настроек Astra Voice — design/spec.md §1.
// Раскладка: [сайдбар 184 | контент] + строка-статус 36 во всю ширину.
// Заголовок и кнопки окна рисует оконный менеджер — свои не рисуем (§1.2).
//
// Файл обязан открываться и БЕЗ контекста Python (`qmlscene qml/Main.qml`):
// appInfo, settingsBridge и themeSource могут отсутствовать — тогда работают дефолты.
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Window 2.15
import QtQuick.Layouts 1.15
import "."
import "." as Av
import "components"
import "onboarding"

ApplicationWindow {
    id: window

    // ── контекст из app.py (может отсутствовать при запуске через qmlscene) ──
    readonly property var info: (typeof appInfo !== "undefined" && appInfo !== null) ? appInfo : null
    readonly property var bridge: (typeof settingsBridge !== "undefined" && settingsBridge !== null) ? settingsBridge : null
    readonly property var updates: (typeof updatesBridge !== "undefined" && updatesBridge !== null) ? updatesBridge : null
    readonly property string appVersion: (info && info.version) ? info.version : "0.1.0"
    readonly property string sessionKind: (info && info.sessionKind) ? info.sessionKind : "OTHER"
    readonly property bool debugVisible: (info && info.debug === true)
    readonly property bool onboardingVisible: (typeof showOnboarding !== "undefined") ? showOnboarding === true : false

    // Ключи переходов из уведомлений соответствуют индексам сайдбара; «Отладка» — после основных разделов.
    readonly property var sectionIndices: {
        var indices = {}
        for (var i = 0; i < sidebar.sections.length; ++i)
            indices[sidebar.sections[i].key] = i
        indices.debug = sidebar.sections.length
        return indices
    }

    // Экран раздела по ключу: и заголовок шапки, и содержимое берутся отсюда.
    readonly property var sectionPages: ({
        "general": "sections/General.qml",
        "models": "sections/Models.qml",
        "output": "sections/Output.qml",
        "network": "sections/Network.qml",
        "advanced": "sections/Advanced.qml",
        "about": "sections/About.qml",
        "debug": "sections/Debug.qml"
    })

    // «Отладка» живёт вне списка сайдбара: у неё свой заголовок и подзаголовок (§1.3).
    readonly property var currentSection: sidebar.debugCurrent
        ? { "key": "debug", "title": qsTr("Отладка"),
            "subtitle": qsTr("Скрытый раздел: Ctrl + Shift + D") }
        : sidebar.sections[sidebar.currentIndex]

    // Плавное появление раздела включается только после сборки окна: при первой
    // загрузке анимировать нечего, а снимок обязан быть одинаковым в любой момент.
    property bool sectionFadeReady: false
    property bool focusModeOnLoad: false
    property bool focusUpdateOnLoad: false

    function showKeyboardMode() {
        focusModeOnLoad = true
        sidebar.currentIndex = window.sectionIndices.general
    }

    function revealPageItem(item) {
        if (!item || !page.item || !body.visible)
            return
        var ancestor = item
        var target = item
        var foundRow = false
        while (ancestor && ancestor !== page.item) {
            // A focused setting control needs its label and explanation in view.
            // SettingRow's public properties identify the nearest containing row.
            if (!foundRow && typeof ancestor.label === "string"
                    && typeof ancestor.sub === "string"
                    && typeof ancestor.divider === "boolean"
                    && typeof ancestor.toggle !== "undefined") {
                target = ancestor
                foundRow = true
            }
            ancestor = ancestor.parent
        }
        if (ancestor !== page.item)
            return
        var margin = Theme.focusOffset + Theme.focusWidth
        // A row taller than the viewport cannot be revealed in full; keep its
        // active control reachable rather than repeatedly jumping between edges.
        if (target.height + margin * 2 > body.height)
            target = item
        var position = target.mapToItem(page, 0, 0)
        if (position.y - margin < body.contentY)
            body.contentY = Math.max(0, position.y - margin)
        else if (position.y + target.height + margin > body.contentY + body.height)
            body.contentY = Math.min(Math.max(0, body.contentHeight - body.height),
                position.y + target.height + margin - body.height)
    }

    onActiveFocusItemChanged: Qt.callLater(function() { window.revealPageItem(window.activeFocusItem) })

    // Снимки экрана обязаны совпадать в любой момент времени: тест выставляет
    // freezeAnimations, и всё, что зависит от хода времени, встаёт на конечную
    // фазу. Так же устроены Onboarding.qml, Pill.qml и DownloadStrip.qml.
    property bool freezeAnimations: false

    width: Theme.sizeWindowW
    height: Theme.sizeWindowMinH
    // 900 × 620 — размер С ДЕКОРАЦИЕЙ KWin (спека §1.2), поэтому минимум клиентской
    // области по высоте — size.window-min-h = 588.
    minimumWidth: Theme.sizeWindowMinW
    minimumHeight: Theme.sizeWindowMinH
    // Окно показывает только Python (focus_shell): при --hidden оно не должно появиться.
    visible: false
    title: window.onboardingVisible ? qsTr("Astra Voice — первый запуск") : qsTr("Astra Voice")
    color: Theme.bgApp
    font.family: Theme.fontUi

    Component.onCompleted: window.sectionFadeReady = true

    Connections {
        target: window.info

        function onShowSection(section) {
            window.show()
            window.raise()
            window.requestActivate()
            if (window.sectionIndices.hasOwnProperty(section))
                sidebar.currentIndex = window.sectionIndices[section]
        }
    }

    Loader {
        anchors.fill: parent
        active: window.onboardingVisible
        sourceComponent: Component { Onboarding {} }
    }

    // ── тело: сайдбар + контент ─────────────────────────────────────────────
    Av.Sidebar {
        id: sidebar
        visible: !window.onboardingVisible
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: downloadStrip.visible ? downloadStrip.top : statusBar.top
        width: Theme.sidebarW
        debugVisible: window.debugVisible
    }

    Item {
        id: content
        visible: !window.onboardingVisible
        anchors.left: sidebar.right
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.bottom: downloadStrip.visible ? downloadStrip.top : statusBar.top

        // Шапка раздела: паддинг 12 22 8 (§1.4). Справа — действие раздела (§5.6).
        RowLayout {
            id: header
            anchors.top: parent.top
            anchors.horizontalCenter: parent.horizontalCenter
            width: Math.min(Theme.sizeContentColMax,
                content.width - 2 * Theme.spaceWindowContentX)
            anchors.topMargin: Theme.spaceWindowContentTop
            spacing: 10 // Макет: зазор между заголовком и кнопкой действия.

            Column {
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                spacing: 2

                Text {
                    textFormat: Text.PlainText
                    text: window.currentSection.key === "advanced" ? qsTr("Продвинутые настройки") : window.currentSection.title
                    color: Theme.fg
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontH2SectionSize
                    font.weight: Font.Bold
                    lineHeight: Theme.fontH2SectionSize * Theme.fontH2SectionLineHeight
                    lineHeightMode: Text.FixedHeight
                    renderType: Text.NativeRendering
                }

                Text {
                    textFormat: Text.PlainText
                    text: window.currentSection.key === "advanced" ? qsTr("Голосовые команды Astra Cowork")
                        : window.currentSection.key === "network" ? qsTr("Сетевые проверки и обновления программы")
                        : window.currentSection.subtitle
                    color: Theme.fgMuted
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSmallSize
                    lineHeight: Theme.fontSmallSize * Theme.fontSmallLineHeight
                    lineHeightMode: Text.FixedHeight
                    renderType: Text.NativeRendering
                }
            }

            // Установка из папки работает всегда, в том числе без сети (§5.6).
            AvButton {
                visible: window.currentSection.key === "models"
                Layout.alignment: Qt.AlignVCenter
                small: true
                iconName: "folder"
                text: qsTr("Установить из файла или папки…")
                enabled: window.bridge !== null
                onClicked: { if (window.bridge) window.bridge.pickInstallPath(); }
            }
        }

        // Тело: паддинг 0 22 16, вертикальная прокрутка, полоса поверх содержимого (§1.4, У3).
        Flickable {
            id: body
            anchors.top: header.bottom
            anchors.topMargin: Theme.spaceHeadGap
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottom: parent.bottom
            width: Math.min(Theme.sizeContentColMax,
                content.width - 2 * Theme.spaceWindowContentX)
            anchors.bottomMargin: Theme.spaceWindowContentBottom
            clip: true
            contentWidth: width
            contentHeight: page.implicitHeight
            boundsBehavior: Flickable.StopAtBounds

            // Полоса поверх содержимого: ширины у колонки не отнимает, появляется только
            // когда содержимое не помещается, и гаснет после прокрутки (§1.4, У3).
            ScrollBar.vertical: ScrollBar {
                id: bodyScroll
                policy: ScrollBar.AsNeeded
                visible: !window.onboardingVisible && size < 1.0
                width: Theme.scrollbarW
                rightPadding: Theme.scrollbarRight

                contentItem: Rectangle {
                    implicitWidth: Theme.scrollbarW
                    radius: Theme.scrollbarRadius
                    color: Theme.scrollbarColor
                    opacity: bodyScroll.active ? Theme.scrollbarOpacity : 0

                    Behavior on opacity {
                        enabled: !window.freezeAnimations

                        NumberAnimation {
                            duration: Theme.durationExit
                            easing.type: Easing.Bezier
                            easing.bezierCurve: Theme.easingExit.concat([1, 1])
                        }
                    }
                }
            }

            Loader {
                id: page
                width: body.width
                source: window.sectionPages[window.currentSection.key]

                onLoaded: {
                    if (window.focusUpdateOnLoad && window.currentSection.key === "network") {
                        window.focusUpdateOnLoad = false;
                        Qt.callLater(function() {
                            if (page.item && typeof page.item.focusUpdatePanel === "function") window.revealPageItem(page.item.focusUpdatePanel());
                        });
                    }
                    if (window.focusModeOnLoad && window.currentSection.key === "general") {
                        window.focusModeOnLoad = false
                        Qt.callLater(function() {
                            if (window.currentSection.key === "general" && page.item)
                                window.revealPageItem(page.item.focusKeyboardMode())
                        })
                    }
                }

                Connections {
                    target: page.item
                    ignoreUnknownSignals: true
                    function onKeyboardModeRequested() { window.showKeyboardMode() }
                }

                onSourceChanged: {
                    // Новый раздел всегда открывается сверху, а не там, где бросили прошлый.
                    body.contentY = 0
                    // Конечная фаза выставляется сразу: незапущенная анимация не
                    // должна оставить раздел полупрозрачным (и на снимке тоже).
                    sectionFade.stop()
                    page.opacity = 1
                    if (window.sectionFadeReady && !window.freezeAnimations)
                        sectionFade.restart()
                }
            }

            NumberAnimation {
                id: sectionFade
                target: page
                property: "opacity"
                from: 0
                to: 1
                duration: Theme.durationEnter
                easing.type: Easing.Bezier
                easing.bezierCurve: Theme.easingEnter.concat([1, 1])
            }
        }
    }

    // Сквозная полоска загрузки — та же, что в мастере (§10.3): очередь одна.
    DownloadStrip {
        id: downloadStrip
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: statusBar.top
        visible: !window.onboardingVisible && downloadState !== "idle" && !doneExpired
        freezeAnimations: window.freezeAnimations
        downloadState: window.bridge ? window.bridge.downloadState : "idle"
        title: window.bridge ? window.bridge.downloadTitle : ""
        downloadCounter: window.bridge ? window.bridge.downloadCounter : ""
        sourceText: window.bridge && window.bridge.downloadSource !== undefined
            ? window.bridge.downloadSource : ""
        progress: window.bridge ? window.bridge.downloadProgress : 0
        speed: window.bridge ? window.bridge.speed : ""
        eta: window.bridge ? window.bridge.eta : ""
        detail: window.bridge ? window.bridge.downloadDetail : ""
        // После неудачи запись выходит из выбора: «Скачать выбранное» её уже не
        // увидит, поэтому полоса повторяет именно записи с ошибкой (§10.3, состояние 4).
        onRetryRequested: { if (window.bridge) window.bridge.retryFailedDownloads(); }
        onOpenFolderRequested: { if (window.bridge) window.bridge.openModelsFolder(); }
    }

    Av.StatusBar {
        id: statusBar
        visible: !window.onboardingVisible
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: Theme.statusbarH
        revocationUnknown: window.bridge ? window.bridge.revocationUnknown : false
        updateState: window.updates ? window.updates.state : "disabled"
        updateNetworkRefusal: window.updates && typeof window.updates.networkRefusal !== "undefined" ? window.updates.networkRefusal : ""
        updateDownloadPhase: window.updates && typeof window.updates.downloadPhase !== "undefined" ? window.updates.downloadPhase : "idle"
        updateDownloadPercent: window.updates && typeof window.updates.downloadPercent !== "undefined" ? window.updates.downloadPercent : 0
        updateVersion: window.updates ? window.updates.version : ""
        updateSnoozed: window.updates ? window.updates.snoozed : false
        updateRestState: window.updates ? window.updates.restState : "idle"
        version: "v" + window.appVersion

        // §6.2: клик по «Доступна версия» открывает панель «Что нового» в разделе «Сеть».
        onUpdateActivated: function(kind) {
            if (kind === "skipped" && window.updates) window.updates.clearSkip();
            window.focusUpdateOnLoad = true;
            if (window.sectionIndices.hasOwnProperty("network")) sidebar.currentIndex = window.sectionIndices["network"];
            if (window.currentSection.key === "network" && page.item && typeof page.item.focusUpdatePanel === "function") {
                window.focusUpdateOnLoad = false;
                window.revealPageItem(page.item.focusUpdatePanel());
            }
        }
    }
    onVisibilityChanged: {
        if (window.bridge && window.bridge.commandPreviewText !== ""
                && (visibility === Window.Hidden || visibility === Window.Minimized))
            window.bridge.rejectCommand()
    }

    AvDialog {
        id: commandPreviewDialog
        heading: qsTr("Команда помощнику")
        message: window.bridge ? window.bridge.commandPreviewText : ""
        note: qsTr("Отправится через 3 секунды. Esc — отмена")
        confirmText: qsTr("Отправить")
        cancelText: qsTr("Не отправлять")
        onOpened: if (window.bridge) window.bridge.commandPreviewShown()
        onConfirmed: if (window.bridge) window.bridge.confirmCommand()
        onCancelled: if (window.bridge) window.bridge.rejectCommand()
    }

    AvDialog {
        id: commandDetailsDialog
        heading: qsTr("Команда помощнику")
        message: window.bridge ? window.bridge.commandDetail : ""
        confirmText: qsTr("Понятно")
        cancelText: ""
    }

    Connections {
        target: window.bridge
        function onCommandPreviewTextChanged() {
            if (window.bridge.commandPreviewText !== "")
                commandPreviewDialog.open()
            else
                commandPreviewDialog.close()
        }
        function onCommandDetailsRequested() {
            sidebar.currentIndex = window.sectionIndices.advanced
            window.show()
            window.raise()
            window.requestActivate()
            commandDetailsDialog.open()
        }
        function onCommandStateChanged() {
            if (!window.bridge.commandDetail)
                commandDetailsDialog.close()
        }
    }
}

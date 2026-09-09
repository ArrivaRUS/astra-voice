// Окно настроек Astra Voice — design/spec.md §1.
// Раскладка: [сайдбар 184 | контент] + строка-статус 36 во всю ширину.
// Заголовок и кнопки окна рисует оконный менеджер — свои не рисуем (§1.2).
//
// Файл обязан открываться и БЕЗ контекста Python (`qmlscene qml/Main.qml`):
// appInfo и themeSource могут отсутствовать — тогда работают дефолты.
import QtQuick 2.15
import QtQuick.Controls 2.15
import "."
import "." as Av
import "sections"

ApplicationWindow {
    id: window

    // ── контекст из app.py (может отсутствовать при запуске через qmlscene) ──
    readonly property var info: (typeof appInfo !== "undefined" && appInfo !== null) ? appInfo : null
    readonly property string appVersion: (info && info.version) ? info.version : "0.1.0"
    readonly property string sessionKind: (info && info.sessionKind) ? info.sessionKind : "OTHER"
    readonly property bool debugVisible: (info && info.debug === true)

    width: Theme.sizeWindowW
    height: Theme.sizeWindowMinH
    // 900 × 620 — размер С ДЕКОРАЦИЕЙ KWin (спека §1.2), поэтому минимум клиентской
    // области по высоте — size.window-min-h = 588.
    minimumWidth: Theme.sizeWindowMinW
    minimumHeight: Theme.sizeWindowMinH
    visible: true
    title: qsTr("Astra Voice")
    color: Theme.bgApp
    font.family: Theme.fontUi

    // ── тело: сайдбар + контент ─────────────────────────────────────────────
    Av.Sidebar {
        id: sidebar
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: statusBar.top
        width: Theme.sidebarW
        debugVisible: window.debugVisible
    }

    Item {
        id: content
        anchors.left: sidebar.right
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.bottom: statusBar.top

        // Шапка раздела: паддинг 12 22 8 (§1.4).
        Column {
            id: header
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: Theme.spaceWindowContentX
            anchors.rightMargin: Theme.spaceWindowContentX
            anchors.topMargin: Theme.spaceWindowContentTop
            bottomPadding: Theme.spaceHeadGap
            spacing: 2

            Text {
                text: qsTr("Общие")
                color: Theme.fg
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontH2SectionSize
                font.weight: Font.Bold
                lineHeight: Theme.fontH2SectionSize * Theme.fontH2SectionLineHeight
                lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
            }

            Text {
                text: qsTr("Диктовка, индикация и запуск")
                color: Theme.fgMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSmallSize
                lineHeight: Theme.fontSmallSize * Theme.fontSmallLineHeight
                lineHeightMode: Text.FixedHeight
                renderType: Text.NativeRendering
            }
        }

        // Тело: паддинг 0 22 16, вертикальная прокрутка, полоса поверх содержимого (§1.4, У3).
        Flickable {
            id: body
            anchors.top: header.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.leftMargin: Theme.spaceWindowContentX
            anchors.rightMargin: Theme.spaceWindowContentX
            anchors.bottomMargin: Theme.spaceWindowContentBottom
            clip: true
            contentWidth: width
            contentHeight: general.implicitHeight
            boundsBehavior: Flickable.StopAtBounds

            // Полоса поверх содержимого: ширины у колонки не отнимает, появляется только
            // когда содержимое не помещается, и гаснет после прокрутки (§1.4, У3).
            ScrollBar.vertical: ScrollBar {
                id: bodyScroll
                policy: ScrollBar.AsNeeded
                visible: size < 1.0
                width: Theme.scrollbarW
                rightPadding: Theme.scrollbarRight

                contentItem: Rectangle {
                    implicitWidth: Theme.scrollbarW
                    radius: Theme.scrollbarRadius
                    color: Theme.scrollbarColor
                    opacity: bodyScroll.active ? Theme.scrollbarOpacity : 0

                    Behavior on opacity {
                        NumberAnimation {
                            duration: Theme.durationExit
                            easing.type: Easing.Bezier
                            easing.bezierCurve: Theme.easingExit.concat([1, 1])
                        }
                    }
                }
            }

            General {
                id: general
                width: body.width
            }
        }
    }

    Av.StatusBar {
        id: statusBar
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: Theme.statusbarH
        version: "v" + window.appVersion
    }
}

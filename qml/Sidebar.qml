// Сайдбар — design/spec.md §1.3. Ширина 184, не тянется.
// Шесть разделов в фиксированном порядке; «Отладка» внизу через распорку — только по флагу.
import QtQuick 2.15
import QtQuick.Layouts 1.15
import "."
import "components"

FocusScope {
    id: root

    property int currentIndex: 0
    property bool debugVisible: false
    property bool debugCurrent: false

    readonly property var sections: [
        { "title": qsTr("Общие"), "icon": "cog", "counter": "" },
        { "title": qsTr("Модели"), "icon": "chip", "counter": "3" },
        { "title": qsTr("Вывод"), "icon": "out", "counter": "" },
        { "title": qsTr("Сеть и обновления"), "icon": "refresh", "counter": "" },
        { "title": qsTr("Продвинутые"), "icon": "sliders", "counter": "" },
        { "title": qsTr("О программе"), "icon": "info", "counter": "" }
    ]

    implicitWidth: Theme.sidebarW

    Rectangle {
        anchors.fill: parent
        color: Theme.sidebarBg

        // Граница справа — 1 px border (§1.3).
        Rectangle {
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: Theme.borderHairline
            color: Theme.border
        }
    }

    Column {
        anchors.fill: parent
        anchors.leftMargin: Theme.sidebarPaddingX
        anchors.rightMargin: Theme.sidebarPaddingX
        anchors.topMargin: Theme.sidebarPaddingY
        anchors.bottomMargin: Theme.sidebarPaddingY

        // ── логотип ────────────────────────────────────────────────────────
        Item {
            width: parent.width
            height: logoRow.height + Theme.sidebarLogoPaddingTop + Theme.sidebarLogoPaddingBottom

            RowLayout {
                id: logoRow
                x: Theme.sidebarLogoPaddingX
                y: Theme.sidebarLogoPaddingTop
                spacing: Theme.titlebarGap

                BrandMark {
                    size: Theme.sidebarLogoMarkW
                    color: Theme.fg
                    // Цвет несёт только третья точка (brand §2.4): в логотипе она акцентная.
                    accentColor: Theme.accent
                    Layout.alignment: Qt.AlignVCenter
                }

                Text {
                    text: "Astra Voice"
                    color: Theme.fg
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.sidebarLogoWordmarkSize
                    font.weight: Font.Medium
                    font.letterSpacing: 0.03 * Theme.sidebarLogoWordmarkSize  // §1.3: трекинг +0.03em
                    renderType: Text.NativeRendering
                    Layout.alignment: Qt.AlignVCenter
                }
            }

            Rectangle {
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                anchors.right: parent.right
                height: Theme.borderHairline
                color: Theme.border
            }
        }

        Item {
            width: parent.width
            height: Theme.sidebarLogoMarginBottom + Theme.sidebarNavMarginTop
        }

        // ── навигация ──────────────────────────────────────────────────────
        Column {
            id: nav
            width: parent.width
            spacing: Theme.sidebarItemGap

            Repeater {
                model: root.sections

                SidebarItem {
                    required property int index
                    required property var modelData

                    width: nav.width
                    title: modelData.title
                    iconName: modelData.icon
                    counter: modelData.counter
                    current: index === root.currentIndex
                    onActivated: root.currentIndex = index
                }
            }
        }
    }

    // ── «Отладка» — прижата к низу распоркой (§1.3) ─────────────────────────
    SidebarItem {
        x: Theme.sidebarPaddingX
        width: root.width - Theme.sidebarPaddingX * 2
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Theme.sidebarPaddingY
        visible: root.debugVisible
        title: qsTr("Отладка (Ctrl+Shift+D)")
        iconName: "sliders"
        muted: true
        current: root.debugCurrent
        onActivated: root.currentIndex = root.sections.length
    }
}

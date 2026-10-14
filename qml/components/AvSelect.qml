// Выпадающий список — design/spec.md §4.4.
// Закрытый: 33,5 высоты, паддинг 6/10, шеврон chevd 13. Раскрытый: не более 6 пунктов (238 px),
// вниз, а если места нет — вверх; выбранный пункт — selection-bg / selection-fg.
// Ширина 236 у поповера — минимальная (§4.4): если названия длиннее поля (микрофоны на
// машинах заказчика 22.09 различаются только хвостом), список расширяется под самое длинное
// название до popupMaxWidth и раскрывается влево, чтобы правый край остался на месте.
// Закрытое поле с обрезанным названием показывает полное подсказкой (§11.3).
import QtQuick 2.15
import QtQuick.Controls 2.15
import ".."

ComboBox {
    id: control

    padding: 0
    leftPadding: Theme.selectPaddingX
    rightPadding: Theme.selectPaddingX
    // design/spec.md §4.4: округляем высоту макета 33,5 вверх до целых 34 px.
    implicitHeight: Math.ceil(Theme.selectHeight)
    font.family: Theme.fontUi
    font.pixelSize: Theme.fontSelectSize

    // Предел ширины раскрытого списка; 0 — не шире самого поля (как в макете).
    property real popupMaxWidth: 0
    // Ширина самого длинного пункта с паддингами; считается при раскрытии.
    property real itemsWidth: 0
    // Optional rich rows keep the native ComboBox keyboard/selection behavior.
    property var itemDetails: []

    function detailsAt(index) {
        return index >= 0 && index < itemDetails.length ? itemDetails[index] : null
    }
    function popupLabel(index) {
        var item = detailsAt(index)
        return item ? (item.popupLabel || item.label || item.name) : textAt(index)
    }
    function accessibleLabel(index) {
        var item = detailsAt(index)
        return item ? (item.group ? item.group + ": " : "") + popupLabel(index) : textAt(index)
    }
    Accessible.name: displayText

    FontMetrics {
        id: itemMetrics
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontMenuItemSize
        font.weight: Font.Medium
    }

    function measureItems() {
        var widest = 0
        for (var i = 0; i < control.count; ++i) {
            widest = Math.max(widest, itemMetrics.advanceWidth(control.popupLabel(i)))
            var item = detailsAt(i)
            if (item && item.subtitle)
                widest = Math.max(widest, itemMetrics.advanceWidth(item.subtitle))
            if (item && item.group)
                widest = Math.max(widest, itemMetrics.advanceWidth(item.group))
        }
        return Math.ceil(widest + Theme.popoverItemPaddingX * 2 + Theme.popoverPadding * 2
                         + Theme.scrollbarW)
    }

    ToolTip {
        id: fullTextTip
        parent: control
        visible: control.hovered && !control.popup.visible && control.contentItem.truncated
        text: control.displayText
        delay: 500
        y: -implicitHeight - 4
        padding: 0
        // §11.3: фон/текст фиксированные (PillTheme), 12 px, паддинг 5/9, радиус 6.
        contentItem: Text {
            textFormat: Text.PlainText
            text: fullTextTip.text
            font.family: Theme.fontUi
            font.pixelSize: 12
            renderType: Text.NativeRendering
            color: PillTheme.tooltipFg
            leftPadding: 9
            rightPadding: 9
            topPadding: 5
            bottomPadding: 5
        }
        background: Rectangle {
            radius: Theme.radiusTooltip
            antialiasing: true
            color: PillTheme.tooltipBg
        }
    }

    contentItem: Text {
        textFormat: Text.PlainText
        text: control.displayText
        font: control.font
        renderType: Text.NativeRendering
        color: !control.enabled ? Theme.fgDisabled
            : control.detailsAt(control.currentIndex) && control.detailsAt(control.currentIndex).muted
                ? Theme.fgMuted : Theme.fg
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
        rightPadding: Theme.selectChevron + Theme.selectGap
    }

    indicator: Icon {
        name: "chevd"
        size: Theme.selectChevron
        color: control.enabled ? Theme.fgMuted : Theme.fgDisabled
        x: control.width - width - Theme.selectPaddingX
        y: (control.height - height) / 2
    }

    background: Rectangle {
        radius: Theme.selectRadius
        antialiasing: true
        color: control.enabled ? Theme.bgSurface : Theme.bgSurface2
        border.width: Theme.fieldBorder
        border.color: control.popup.visible ? Theme.selectOpenState : Theme.border

        Rectangle {
            anchors.fill: parent
            anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
            radius: Theme.focusRadius
            color: "transparent"
            border.width: Theme.focusWidth
            border.color: Theme.stateFocusRing
            antialiasing: true
            visible: control.visualFocus
        }
    }

    // The heading is a sibling of the clickable option, outside its hit area.
    // The delegate model still contains devices only, so keyboard indices stay native.
    delegate: Item {
        id: row
        required property int index
        required property string modelData

        readonly property var details: control.detailsAt(index)
        readonly property bool groupStart: details && details.group
            && (index === 0 || !control.detailsAt(index - 1)
                || control.detailsAt(index - 1).group !== details.group)
        readonly property real subtitleHeight: details && details.subtitle
            ? Math.ceil(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight) : 0
        readonly property real groupHeight: groupStart ? Theme.popoverItemH : 0
        width: control.popup.width - Theme.popoverPadding * 2
        height: groupHeight + Theme.popoverItemH + subtitleHeight

        Item {
            id: groupHeading
            visible: row.groupStart
            width: parent.width
            height: row.groupHeight

            Rectangle {
                objectName: "selectGroupDivider"
                width: parent.width
                height: 1
                color: Theme.border
            }
            Text {
                objectName: "selectGroupHeader"
                x: Theme.popoverItemPaddingX
                width: parent.width - Theme.popoverItemPaddingX * 2
                height: parent.height
                text: row.details && row.details.group ? row.details.group.toUpperCase() : ""
                textFormat: Text.PlainText
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontGroupCapsSize
                font.weight: Font.Medium
                font.capitalization: Font.AllUppercase
                font.letterSpacing: Theme.fontGroupCapsTracking * Theme.fontGroupCapsSize
                renderType: Text.NativeRendering
                color: Theme.fgMuted
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
                Accessible.role: Accessible.StaticText
                Accessible.name: text
                Accessible.focusable: false
            }
            MouseArea {
                anchors.fill: parent
                // Consume pointer events without activating or focusing a device.
                onPressed: mouse.accepted = true
                onReleased: mouse.accepted = true
                onClicked: mouse.accepted = true
            }
        }

        ItemDelegate {
            id: option
            objectName: "selectOption"
            readonly property int deviceIndex: row.index
            y: row.groupHeight
            width: parent.width
            height: Theme.popoverItemH + row.subtitleHeight
            padding: 0
            leftPadding: Theme.popoverItemPaddingX
            rightPadding: Theme.popoverItemPaddingX
            focusPolicy: Qt.NoFocus
            Accessible.name: control.accessibleLabel(row.index)
            onClicked: {
                control.currentIndex = row.index
                control.activated(row.index)
                control.popup.close()
            }

            contentItem: Column {
                Text {
                    objectName: "selectItemLabel"
                    width: parent.width
                    height: Theme.popoverItemH
                    textFormat: Text.PlainText
                    text: control.popupLabel(row.index)
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontMenuItemSize
                    font.weight: row.index === control.currentIndex ? Font.Medium : Font.Normal
                    renderType: Text.NativeRendering
                    color: row.details && row.details.muted ? Theme.fgMuted
                        : row.index === control.currentIndex ? Theme.selectionFg : Theme.fg
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
                Text {
                    objectName: "selectItemSubtitle"
                    width: parent.width
                    height: row.subtitleHeight
                    visible: height > 0
                    text: row.details && row.details.subtitle || ""
                    textFormat: Text.PlainText
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSettingSubSize
                    lineHeight: Math.ceil(Theme.fontSettingSubSize * Theme.fontSettingSubLineHeight)
                    lineHeightMode: Text.FixedHeight
                    renderType: Text.NativeRendering
                    color: Theme.fgMuted
                    elide: Text.ElideRight
                }
            }

            background: Rectangle {
                radius: Theme.popoverItemRadius
                antialiasing: true
                color: row.index === control.currentIndex ? Theme.selectionBg
                     : (option.hovered ? Theme.stateHoverOnSurface : "transparent")
            }
        }
    }

    popup: Popup {
        y: control.height
        width: control.popupMaxWidth > 0
               ? Math.max(control.width, Math.min(control.popupMaxWidth, control.itemsWidth))
               : control.width
        // Правый край на месте: широкий список уходит влево, к подписи строки.
        x: control.width - width
        onAboutToShow: control.itemsWidth = control.measureItems()
        implicitHeight: Math.min(listView.contentHeight + Theme.popoverPadding * 2, Theme.popoverMaxH)
        padding: Theme.popoverPadding

        contentItem: ListView {
            id: listView
            clip: true
            implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            boundsBehavior: Flickable.StopAtBounds

            ScrollBar.vertical: ScrollBar {
                id: listScroll
                policy: ScrollBar.AsNeeded
                visible: size < 1.0
                width: Theme.scrollbarW

                contentItem: Rectangle {
                    implicitWidth: Theme.scrollbarW
                    radius: Theme.scrollbarRadius
                    color: Theme.scrollbarColor
                    opacity: listScroll.active ? Theme.scrollbarOpacity : 0

                    Behavior on opacity {
                        NumberAnimation {
                            duration: Theme.durationExit
                            easing.type: Easing.Bezier
                            easing.bezierCurve: Theme.easingExit.concat([1, 1])
                        }
                    }
                }
            }
        }

        background: Rectangle {
            radius: Theme.popoverRadius
            antialiasing: true
            color: Theme.bgSurface
            border.width: Theme.fieldBorder
            border.color: Theme.border
        }
    }
}

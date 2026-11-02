// W3 «Предупредите участников о записи» — presentation only.
// Copy, close and window focus belong to ui/meeting_notice.py.
// Geometry: design/meetings/tokens.md §5.3 (PillTheme.pillMeetingNotice*),
// shared W1/W3 header and buttons (Theme.notification*, PillTheme.pillMeetingOffer*).
import QtQuick 2.15
import "."
import "components"

FocusScope {
    id: root
    objectName: "meetingNotice"
    property var presentation: ({})
    property var texts: ({})
    property bool keyboardMode: false
    signal copyClicked()
    signal closeClicked()
    signal keyboardRequested()
    signal copySelectionRequested(string text)
    readonly property string copyState: presentation.copyState || "default"
    readonly property bool admin: presentation.admin === true
    readonly property real lineFactor: PillTheme.pillMeetingOfferBodyLineHeight
    readonly property real borderWidth: PillTheme.pillMeetingNoticeTextboxBorder
    readonly property real contentX: borderWidth + PillTheme.pillMeetingNoticePaddingX
    readonly property real contentY: borderWidth + PillTheme.pillMeetingNoticePaddingY
    readonly property real contentWidth: PillTheme.pillMeetingNoticeW - contentX * 2
    readonly property real surfaceWidth: PillTheme.pillMeetingNoticeW
    readonly property real surfaceHeight: foot.y + foot.height + PillTheme.pillMeetingNoticePaddingY + borderWidth
    width: surfaceWidth
    height: surfaceHeight
    implicitWidth: surfaceWidth
    implicitHeight: surfaceHeight
    visible: presentation.visible === true
    Accessible.role: Accessible.Dialog
    Accessible.name: texts.title || ""
    Accessible.description: presentation.text || ""
    Keys.onEscapePressed: {
        root.closeClicked();
        event.accepted = true;
    }

    function forceLayout() {
        headerText.forceLayout();
        title.forceLayout();
        adminText.forceLayout();
        copyText.forceLayout();
        hintText.forceLayout();
        muteText.forceLayout();
    }
    function focusFirstControl() { copyButton.forceActiveFocus(); }
    function focusText() { noticeText.forceActiveFocus(); }
    // Untrusted administrator text: escaped, rich text only carries CSS line-height
    // (TextEdit has no lineHeight in Qt 5; spec line-height 1.45).
    function richText(value) {
        return "<div style=\"line-height:" + (PillTheme.pillMeetingNoticeTextboxSize * root.lineFactor) + "px\">"
            + String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;").replace(/\n/g, "<br>")
            + "</div>";
    }

    OverlaySurface {
        surfaceWidth: root.surfaceWidth
        surfaceHeight: root.surfaceHeight
        surfaceRadius: PillTheme.pillMeetingNoticeRadius
        // shadow.notify: 0 14px 36px rgba(0,0,0,.5)
        shadowY: 14
        shadowRadius: 36
        shadowColor: Qt.rgba(0, 0, 0, 0.5)
        surfaceColor: PillTheme.notifyBg
        borderColor: PillTheme.notifyBorder
        borderWidth: root.borderWidth
    }

    Item {
        id: head
        x: root.contentX
        y: root.contentY
        width: root.contentWidth
        height: PillTheme.pillMeetingOfferClose
        Icon {
            size: PillTheme.pillMeetingNoticeHeadIcon
            y: (head.height - height) / 2
            name: "chat"
            color: PillTheme.notifyFgMuted
        }
        Item {
            x: PillTheme.pillMeetingNoticeHeadIcon + PillTheme.pillMeetingOfferHeadGap
            width: headerText.implicitWidth * headerText.scale
            height: Theme.notificationHeaderSize * root.lineFactor
            y: (head.height - height) / 2
            Text {
                id: headerText
                textFormat: Text.PlainText
                text: root.texts.header || ""
                color: PillTheme.notifyFgMuted
                font.family: "PT Root UI"
                // Qt 5 rounds pixelSize to int: lay out at 2x and scale.
                font.pixelSize: Theme.notificationHeaderSize * 2
                lineHeightMode: Text.FixedHeight
                lineHeight: Theme.notificationHeaderSize * root.lineFactor * 2
                scale: 0.5
                transformOrigin: Item.TopLeft
            }
        }
        Rectangle {
            id: closeButton
            objectName: "meetingNoticeClose"
            x: head.width - width
            width: PillTheme.pillMeetingOfferClose
            height: width
            radius: width / 2
            color: PillTheme.notifyBtnBg
            activeFocusOnTab: root.keyboardMode
            border.width: activeFocus ? 2 : 0
            border.color: PillTheme.pillFg
            Accessible.role: Accessible.Button
            Accessible.name: root.texts.close || ""
            Accessible.onPressAction: root.closeClicked()
            Keys.onReturnPressed: root.closeClicked()
            Keys.onEnterPressed: root.closeClicked()
            Keys.onSpacePressed: root.closeClicked()
            Icon {
                anchors.centerIn: parent
                size: PillTheme.pillMeetingOfferCloseIcon
                name: "x"
                color: PillTheme.pillCloseFg
            }
            MouseArea { anchors.fill: parent; onClicked: root.closeClicked() }
        }
    }

    Item {
        id: titleBox
        x: root.contentX
        y: head.y + head.height + PillTheme.pillMeetingOfferHeadMarginBottom
        width: root.contentWidth
        height: title.implicitHeight * title.scale
        Text {
            id: title
            objectName: "meetingNoticeTitle"
            textFormat: Text.PlainText
            width: parent.width / scale
            wrapMode: Text.Wrap
            text: root.texts.title || ""
            color: PillTheme.notifyFg
            font.family: "PT Root UI"
            font.pixelSize: Theme.notificationTitleSize * 2
            font.weight: Font.Medium
            lineHeightMode: Text.FixedHeight
            lineHeight: Theme.notificationTitleSize * root.lineFactor * 2
            scale: 0.5
            transformOrigin: Item.TopLeft
        }
    }

    Rectangle {
        id: box
        objectName: "meetingNoticeBox"
        x: root.contentX
        // CSS margins collapse: title 3 / textbox 8 → 8.
        y: titleBox.y + titleBox.height + Math.max(PillTheme.pillMeetingOfferTitleMarginBottom, PillTheme.pillMeetingNoticeTextboxMarginTop)
        width: root.contentWidth
        height: noticeText.contentHeight + 2 * (root.borderWidth + PillTheme.pillMeetingNoticeTextboxPaddingY)
        color: "transparent"
        radius: PillTheme.pillMeetingNoticeTextboxRadius
        border.width: root.borderWidth
        border.color: PillTheme.notifyBorder
        TextEdit {
            id: noticeText
            objectName: "meetingNoticeText"
            x: root.borderWidth + PillTheme.pillMeetingNoticeTextboxPaddingX
            y: root.borderWidth + PillTheme.pillMeetingNoticeTextboxPaddingY
            width: box.width - 2 * x
            readOnly: true
            selectByMouse: true
            selectByKeyboard: true
            persistentSelection: true
            activeFocusOnTab: root.keyboardMode
            wrapMode: TextEdit.Wrap
            textFormat: TextEdit.RichText
            text: root.richText(root.presentation.text || "")
            color: PillTheme.notifyFg
            selectionColor: PillTheme.notifyBtnPrimaryBg
            selectedTextColor: PillTheme.notifyBtnPrimaryFg
            font.family: "PT Root UI"
            font.pixelSize: PillTheme.pillMeetingNoticeTextboxSize
            Accessible.role: Accessible.StaticText
            Accessible.name: root.presentation.text || ""
            // Manual copy needs keyboard focus (Ctrl+C); only an explicit selection asks for it.
            onSelectedTextChanged: if (selectedText !== "" && !root.keyboardMode) root.keyboardRequested()
            // TextEdit's own copy of rich text publishes text/html and ODF too:
            // the chat must receive text/plain only (copy_plain_text in Python).
            Keys.onPressed: {
                if (event.matches(StandardKey.Copy)) {
                    if (selectedText !== "") root.copySelectionRequested(selectedText);
                    event.accepted = true;
                }
            }
        }
    }

    Item {
        id: adminLine
        objectName: "meetingNoticeAdmin"
        visible: root.admin
        x: root.contentX
        // textbox margin-bottom 2 collapses with the admin line margin-top 4.
        y: box.y + box.height + Math.max(PillTheme.pillMeetingNoticeTextboxMarginBottom, PillTheme.pillMeetingNoticeAdminMarginTop)
        width: root.contentWidth
        height: visible ? adminText.implicitHeight * adminText.scale : 0
        Text {
            id: adminText
            textFormat: Text.PlainText
            width: parent.width / scale
            wrapMode: Text.Wrap
            text: root.texts.admin || ""
            color: PillTheme.notifyFgMuted
            font.family: "PT Root UI"
            font.pixelSize: PillTheme.pillMeetingNoticeAdminSize * 2
            lineHeightMode: Text.FixedHeight
            lineHeight: PillTheme.pillMeetingNoticeAdminSize * root.lineFactor * 2
            scale: 0.5
            transformOrigin: Item.TopLeft
        }
    }

    Rectangle {
        id: copyButton
        objectName: "meetingNoticeCopy"
        readonly property bool primary: root.copyState === "default"
        readonly property real iconSize: PillTheme.pillMeetingOfferBtnIcon
        readonly property real gap: 6
        readonly property real naturalWidth: copyText.implicitWidth * copyText.scale + iconSize + gap + 2 * Theme.notificationButtonPaddingX
        x: root.contentX
        // The textbox margin-bottom 2 collapses into the buttons margin-top 10.
        y: (root.admin ? adminLine.y + adminLine.height : box.y + box.height) + PillTheme.pillMeetingOfferButtonsMarginTop
        width: Math.min(root.contentWidth, naturalWidth)
        height: copyLabel.height + 2 * Theme.notificationButtonPaddingY
        radius: Theme.notificationButtonRadius
        color: primary ? PillTheme.notifyBtnPrimaryBg : PillTheme.notifyBtnBg
        activeFocusOnTab: root.keyboardMode
        border.width: activeFocus ? 2 : 0
        border.color: PillTheme.pillFg
        Accessible.role: Accessible.Button
        Accessible.name: copyText.text
        Accessible.onPressAction: root.copyClicked()
        Keys.onReturnPressed: root.copyClicked()
        Keys.onEnterPressed: root.copyClicked()
        Keys.onSpacePressed: root.copyClicked()
        Icon {
            x: Theme.notificationButtonPaddingX
            y: (copyButton.height - height) / 2
            size: copyButton.iconSize
            name: root.copyState === "copied" ? "check" : root.copyState === "failed" ? "alert" : "copy"
            color: root.copyState === "failed" ? PillTheme.pillError : copyButton.primary ? PillTheme.notifyBtnPrimaryFg : PillTheme.notifyFg
        }
        Item {
            id: copyLabel
            x: Theme.notificationButtonPaddingX + copyButton.iconSize + copyButton.gap
            y: Theme.notificationButtonPaddingY
            width: copyButton.width - x - Theme.notificationButtonPaddingX
            height: copyText.implicitHeight * copyText.scale
            Text {
                id: copyText
                objectName: "meetingNoticeCopyText"
                textFormat: Text.PlainText
                width: parent.width / scale
                wrapMode: Text.Wrap
                text: root.copyState === "copied" ? (root.texts.copied || "") : root.copyState === "failed" ? (root.texts.copyFailed || "") : (root.texts.copy || "")
                color: copyButton.primary ? PillTheme.notifyBtnPrimaryFg : PillTheme.notifyFg
                font.family: "PT Root UI"
                font.pixelSize: PillTheme.pillMeetingOfferBodySize * 2
                font.weight: copyButton.primary ? Font.Medium : Font.Normal
                lineHeightMode: Text.FixedHeight
                lineHeight: PillTheme.pillMeetingOfferBodySize * root.lineFactor * 2
                scale: 0.5
                transformOrigin: Item.TopLeft
            }
        }
        MouseArea { anchors.fill: parent; onClicked: root.copyClicked() }
    }

    Item {
        id: hint
        x: root.contentX
        y: copyButton.y + copyButton.height + PillTheme.pillMeetingNoticeHintMarginTop
        width: root.contentWidth
        height: hintText.implicitHeight * hintText.scale
        Text {
            id: hintText
            textFormat: Text.PlainText
            width: parent.width / scale
            wrapMode: Text.Wrap
            text: root.texts.hint || ""
            color: PillTheme.notifyFgMuted
            font.family: "PT Root UI"
            font.pixelSize: PillTheme.pillMeetingOfferBodySize * 2
            lineHeightMode: Text.FixedHeight
            lineHeight: PillTheme.pillMeetingOfferBodySize * root.lineFactor * 2
            scale: 0.5
            transformOrigin: Item.TopLeft
        }
    }

    Item {
        id: foot
        objectName: "meetingNoticeMute"
        x: root.contentX
        y: hint.y + hint.height + PillTheme.pillMeetingNoticeFootMarginTop
        width: root.contentWidth
        height: muteBox.y + muteBox.height
        Rectangle { width: parent.width; height: root.borderWidth; color: PillTheme.notifyBorder }
        Item {
            id: muteBox
            y: root.borderWidth + PillTheme.pillMeetingNoticeFootPaddingTop
            width: parent.width
            height: muteText.implicitHeight * muteText.scale
            Text {
                id: muteText
                objectName: "meetingNoticeMuteText"
                textFormat: Text.PlainText
                width: parent.width / scale
                wrapMode: Text.Wrap
                text: root.presentation.muteText || ""
                color: PillTheme.notifyFgMuted
                font.family: "PT Root UI"
                font.pixelSize: PillTheme.pillMeetingOfferBodySize * 2
                lineHeightMode: Text.FixedHeight
                lineHeight: PillTheme.pillMeetingOfferBodySize * root.lineFactor * 2
                scale: 0.5
                transformOrigin: Item.TopLeft
            }
        }
    }
}

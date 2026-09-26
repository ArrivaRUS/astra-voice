// Обрамление мастера — design/spec.md §10.3; design/mockups/final/_shell.py: dlbar().
import QtQuick 2.15
import QtQuick.Layouts 1.15
import ".."

Rectangle {
    id: root

    property string downloadState: "idle" // idle | downloading | verifying | done | failed | no-space
    property string title: ""
    property string downloadCounter: ""
    property real progress: 0
    property string sourceText: ""
    property string speed: ""
    property string eta: ""
    // Уточнение к заголовку: «нужно ещё 126 МБ» при нехватке места (§10.3).
    property string detail: ""
    property bool freezeAnimations: false

    signal retryRequested()
    signal openFolderRequested()

    property bool doneExpired: false
    readonly property bool hasError: downloadState === "failed" || downloadState === "no-space"
    readonly property string tail: [sourceText, speed,
        downloadState === "downloading" && speed === "" && eta === "" ? qsTr("считаю…") : eta
    ].filter(function(part) {
        return part !== "";
    }).join(qsTr(" · "))

    implicitHeight: Theme.onboardingProgressStripH
    width: parent ? parent.width : implicitWidth
    height: implicitHeight
    color: Theme.onboardingProgressStripBg
    visible: downloadState !== "idle" && !doneExpired

    function resetDoneDisplay() {
        doneTimer.stop();
        doneExpired = false;
        if (downloadState === "done" && !freezeAnimations)
            doneTimer.start();
    }

    onDownloadStateChanged: resetDoneDisplay()
    onFreezeAnimationsChanged: resetDoneDisplay()
    Component.onCompleted: resetDoneDisplay()

    Timer {
        id: doneTimer
        interval: 3000
        repeat: false
        onTriggered: root.doneExpired = true
    }

    Rectangle {
        anchors.top: parent.top
        width: parent.width
        height: Theme.borderDivider
        color: Theme.border
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: Theme.spaceWindowContentX
        anchors.rightMargin: Theme.spaceWindowContentX
        spacing: Theme.onboardingProgressStripGap

        Icon {
            size: Theme.onboardingProgressStripIcon
            Layout.minimumWidth: size
            name: root.downloadState === "downloading" ? "down"
                : root.downloadState === "verifying" ? "refresh"
                : root.downloadState === "done" ? "check" : "alert"
            color: root.downloadState === "downloading" ? Theme.primary
                : root.downloadState === "verifying" ? Theme.fgMuted
                : root.downloadState === "done" ? Theme.successInk : Theme.dangerInk
        }

        Text {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.maximumWidth: implicitWidth
            text: root.title
            textFormat: Text.PlainText
            renderType: Text.NativeRendering
            font.family: Theme.fontUi
            font.pixelSize: Theme.onboardingProgressStripTextSize
            wrapMode: Text.NoWrap
            elide: Text.ElideRight
            color: root.downloadState === "done" ? Theme.successInk
                : root.hasError ? Theme.dangerInk : Theme.onboardingProgressStripTextColor
        }

        Text {
            objectName: "downloadCounter"
            visible: root.downloadState === "downloading" && root.downloadCounter !== ""
            Layout.minimumWidth: visible ? implicitWidth : 0
            text: root.downloadCounter
            textFormat: Text.PlainText
            renderType: Text.NativeRendering
            font.family: Theme.fontUi
            font.pixelSize: Theme.onboardingProgressStripTailSize
            color: Theme.fgMuted
        }

        Text {
            objectName: "downloadDetail"
            visible: root.downloadState === "no-space" && root.detail !== ""
            Layout.minimumWidth: visible ? implicitWidth : 0
            text: root.detail
            textFormat: Text.PlainText
            renderType: Text.NativeRendering
            font.family: Theme.fontUi
            font.pixelSize: Theme.onboardingProgressStripTailSize
            wrapMode: Text.NoWrap
            color: Theme.onboardingProgressStripTailColor
        }

        Item {
            Layout.fillWidth: true
        }

        Rectangle {
            id: track
            property real slide: 0
            visible: root.downloadState === "downloading" || root.downloadState === "verifying"
            Layout.minimumWidth: Theme.onboardingProgressStripTrackW
            Layout.maximumWidth: Theme.onboardingProgressStripTrackW
            Layout.preferredWidth: Theme.onboardingProgressStripTrackW
            Layout.preferredHeight: Theme.onboardingProgressStripTrackH
            radius: Theme.progressRadius
            color: Theme.bgSurface2
            clip: true

            Rectangle {
                visible: root.downloadState === "downloading"
                width: track.width * Math.max(0, Math.min(1, root.progress))
                height: track.height
                radius: Theme.progressRadius
                color: Theme.primary
            }

            Rectangle {
                id: segment
                x: indeterminateAnimation.running ? track.slide : track.width * 0.26
                visible: root.downloadState === "verifying"
                width: track.width * 0.38
                height: track.height
                radius: Theme.progressRadius
                color: Theme.primary
            }

            NumberAnimation {
                id: indeterminateAnimation
                target: track
                property: "slide"
                from: -segment.width
                to: track.width
                duration: 1200
                easing.type: Easing.Linear
                loops: Animation.Infinite
                running: root.visible && root.downloadState === "verifying" && !root.freezeAnimations
            }
        }

        Text {
            visible: root.downloadState === "downloading" && root.tail !== ""
            Layout.minimumWidth: implicitWidth
            text: root.tail
            textFormat: Text.PlainText
            renderType: Text.NativeRendering
            font.family: Theme.fontUi
            font.pixelSize: Theme.onboardingProgressStripTailSize
            wrapMode: Text.NoWrap
            color: Theme.onboardingProgressStripTailColor
        }

        AvButton {
            visible: root.downloadState === "failed"
            Layout.minimumWidth: implicitWidth
            small: true
            iconName: "refresh"
            text: qsTr("Повторить")
            onClicked: root.retryRequested()
        }

        AvButton {
            visible: root.downloadState === "no-space"
            Layout.minimumWidth: implicitWidth
            small: true
            iconName: "folder"
            text: qsTr("Открыть папку моделей")
            onClicked: root.openFolderRequested()
        }
    }
}

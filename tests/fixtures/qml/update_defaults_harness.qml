import QtQuick 2.15
import QtQuick.Window 2.15
import "../../../qml/sections" as Sections
import "../../../qml/onboarding" as Steps

Window {
    width: 800
    height: 600
    visible: true
    property string activeView: "network"
    Sections.Network {
        width: parent.width
        visible: activeView === "network"
    }
    Sections.About {
        objectName: "aboutContractSection"
        width: parent.width
        visible: activeView === "about"
    }
    Steps.Step1Network {
        width: 580
        visible: activeView === "onboarding"
    }
}

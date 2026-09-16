// design/mockups/final/08-onboarding-4-mic.html: .lvl; design/spec.md §8.3.
import QtQuick 2.15
import ".."

Item {
    id: root

    property real level: 0
    property bool silent: false

    implicitWidth: Theme.micLevelMeterBars * Theme.micLevelMeterBarW
                   + (Theme.micLevelMeterBars - 1) * Theme.micLevelMeterBarGap
    implicitHeight: Theme.micLevelMeterH

    Repeater {
        model: Theme.micLevelMeterBars

        Rectangle {
            required property int index

            x: index * (Theme.micLevelMeterBarW + Theme.micLevelMeterBarGap)
            y: root.height - height
            width: Theme.micLevelMeterBarW
            height: {
                // §8.3: минимальная высота 3 px по заданию и плоскому состоянию макета.
                if (root.silent || root.level <= 0)
                    return 3
                // .lvl в макете (§8.3): образец задан прямо в пикселях макета.
                return Math.min(Theme.micLevelMeterH, Math.max(3,
                    Math.round(Theme.micLevelMeterSampleLive[index] * root.level)))
            }
            radius: Theme.micLevelMeterBarRadius
            color: root.silent ? Theme.micLevelMeterColorFlat : Theme.micLevelMeterColorLive
            antialiasing: true
        }
    }
}

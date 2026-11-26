import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasma5support as P5Support

PlasmoidItem {
    id: root

    preferredRepresentation: fullRepresentation
    Plasmoid.backgroundHints: PlasmaCore.Types.NoBackground

    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: "Quit Appy"
            icon.name: "application-exit"
            visible: root.connected
            onTriggered: {
                var xhr = new XMLHttpRequest();
                xhr.open("POST", "http://127.0.0.1:8002/quit");
                xhr.send();
            }
        }
    ]

    property string appyState: "idle"
    property string appyExpression: "neutral"
    property int msgCount: 0
    property bool minimized: false
    property bool connected: false
    property real phase: 0.0
    property real talkPhase: 0.0
    property real blinkScale: 1.0
    // Clicked while Appy was off: starting, until the overlay answers (or LAUNCH_TIMEOUT)
    property bool launching: false
    // When the current boot began, for the loading bar's fill (ms since epoch; 0 = none)
    property real bootStart: 0

    // The pill is always in the panel: asleep while Appy is off, click to start him
    readonly property bool showPill: true
    readonly property bool asleep: !connected && !launching
    readonly property bool isBooting: (launching && !connected) || (connected && appyState === "booting")
    readonly property bool isBusy: isBooting || (connected && (appyState === "thinking" || appyState === "compacting"))
    readonly property bool isSpeaking: connected && appyState === "speaking"
    readonly property string launchCommand: 'mkdir -p "$HOME/.cache/appy" && setsid -f "$HOME/.local/bin/appy" --no-build >"$HOME/.cache/appy/appy.log" 2>&1 </dev/null'

    // Same curve as the overlay's mascot (mascot.rs loading_fill): a stub that eases
    // towards 95%, since booting reports no real progress
    function loadingFill() {
        var secs = bootStart > 0 ? (Date.now() - bootStart) / 1000 : 0;
        return 0.08 + 0.87 * (1 - Math.exp(-secs / 3));
    }

    function launch() {
        launching = true;
        bootStart = Date.now();
        launchTimeout.restart();
        launcher.connectSource(launchCommand);
    }

    P5Support.DataSource {
        id: launcher
        engine: "executable"
        connectedSources: []
        onNewData: function(source, data) { disconnectSource(source); }
    }

    Timer {
        id: launchTimeout
        interval: 60000
        repeat: false
        onTriggered: root.launching = false
    }

    onConnectedChanged: {
        if (connected) {
            launching = false;
            launchTimeout.stop();
        } else if (!launching) {
            bootStart = 0;
        }
        if (fullRepresentationItem && fullRepresentationItem.faceCanvas) {
            fullRepresentationItem.faceCanvas.requestPaint();
        }
    }

    Timer {
        interval: 200
        running: true
        repeat: true
        onTriggered: {
            var xhr = new XMLHttpRequest();
            xhr.open("GET", "http://127.0.0.1:8002/state");
            xhr.onreadystatechange = function() {
                if (xhr.readyState === XMLHttpRequest.DONE) {
                    if (xhr.status === 200) {
                        try {
                            var data = JSON.parse(xhr.responseText);
                            root.connected = true;
                            root.appyState = data.state || "idle";
                            root.appyExpression = data.expression || "neutral";
                            root.msgCount = data.msg_count || 0;
                            root.minimized = !!data.minimized;
                        } catch (e) {}
                    } else {
                        root.connected = false;
                        root.minimized = false;
                        root.msgCount = 0;
                    }
                }
            };
            xhr.send();
        }
    }

    // 60fps animation when busy or speaking
    Timer {
        interval: 16
        running: root.isBusy || root.isSpeaking
        repeat: true
        onTriggered: {
            if (root.isBusy) {
                root.phase = (root.phase + 0.14) % (Math.PI * 2);
            }
            if (root.isSpeaking) {
                root.talkPhase = (root.talkPhase + 0.28) % (Math.PI * 20);
            }
            if (root.fullRepresentationItem && root.fullRepresentationItem.faceCanvas) {
                root.fullRepresentationItem.faceCanvas.requestPaint();
            }
        }
    }

    // Natural human blinking
    Timer {
        id: blinkTrigger
        interval: 3400
        running: !root.asleep && !root.isBusy && root.appyState !== "listening"
        repeat: false
        onTriggered: {
            blinkAnim.restart();
            interval = 2800 + Math.floor(Math.random() * 2600);
            restart();
        }
    }

    SequentialAnimation {
        id: blinkAnim
        NumberAnimation {
            target: root
            property: "blinkScale"
            to: 0.08
            duration: 75
            easing.type: Easing.InQuad
        }
        NumberAnimation {
            target: root
            property: "blinkScale"
            to: 1.0
            duration: 85
            easing.type: Easing.OutQuad
        }
    }

    onBlinkScaleChanged: {
        if (fullRepresentationItem && fullRepresentationItem.faceCanvas) {
            fullRepresentationItem.faceCanvas.requestPaint();
        }
    }

    onAppyStateChanged: {
        if (appyState === "booting" && bootStart === 0) {
            bootStart = Date.now();
        } else if (appyState !== "booting" && connected) {
            bootStart = 0;
        }
        if (isBusy) {
            blinkAnim.stop();
            blinkScale = 1.0;
        }
        if (fullRepresentationItem && fullRepresentationItem.faceCanvas) {
            fullRepresentationItem.faceCanvas.requestPaint();
        }
    }

    onAppyExpressionChanged: {
        if (fullRepresentationItem && fullRepresentationItem.faceCanvas) {
            fullRepresentationItem.faceCanvas.requestPaint();
        }
    }

    fullRepresentation: Item {
        id: container
        property alias faceCanvas: canvas

        visible: root.showPill
        Layout.minimumWidth: root.showPill ? pill.implicitWidth : 0
        Layout.preferredWidth: root.showPill ? pill.implicitWidth : 0
        Layout.maximumWidth: root.showPill ? pill.implicitWidth : 0
        Layout.fillHeight: true

        Rectangle {
            id: pill
            anchors.verticalCenter: parent.verticalCenter
            height: Math.min(parent.height - 6, 34)
            implicitWidth: row.implicitWidth + (root.msgCount > 0 ? 14 : 10)
            width: implicitWidth
            radius: height / 2
            color: "#000000"
            border.width: 0

            RowLayout {
                id: row
                anchors.centerIn: parent
                spacing: root.msgCount > 0 ? 6 : 0

                Canvas {
                    id: canvas
                    Layout.preferredWidth: 26
                    Layout.preferredHeight: 26
                    opacity: root.asleep ? 0.6 : 1.0

                    function roundedRect(ctx, x, y, w, h, r) {
                        var rad = Math.min(r, w * 0.5, h * 0.5);
                        ctx.beginPath();
                        ctx.moveTo(x + rad, y);
                        ctx.arcTo(x + w, y, x + w, y + h, rad);
                        ctx.arcTo(x + w, y + h, x, y + h, rad);
                        ctx.arcTo(x, y + h, x, y, rad);
                        ctx.arcTo(x, y, x + w, y, rad);
                        ctx.closePath();
                    }

                    onPaint: {
                        var ctx = getContext("2d");
                        var w = width;
                        var h = height;
                        ctx.clearRect(0, 0, w, h);

                        ctx.save();
                        if (root.isSpeaking) {
                            var bounce = Math.abs(Math.sin(root.talkPhase)) * 2.2;
                            ctx.translate(0, -bounce);
                        }

                        // 1. Squircle body
                        roundedRect(ctx, w * 0.10, h * 0.22, w * 0.80, h * 0.72, w * 0.24);
                        ctx.fillStyle = "rgba(218, 216, 208, 0.94)";
                        ctx.fill();

                        // 2. Blush-white leaf
                        ctx.beginPath();
                        ctx.moveTo(w * 0.50, h * 0.23);
                        ctx.bezierCurveTo(w * 0.52, h * 0.06, w * 0.58, h * 0.05, w * 0.67, h * 0.04);
                        ctx.bezierCurveTo(w * 0.66, h * 0.16, w * 0.60, h * 0.21, w * 0.50, h * 0.23);
                        ctx.closePath();
                        ctx.fillStyle = "rgba(251, 242, 242, 0.96)";
                        ctx.fill();

                        // 3. Face
                        var cy = h * 0.59;
                        ctx.fillStyle = "rgb(20, 20, 24)";
                        ctx.strokeStyle = "rgb(20, 20, 24)";

                        if (root.isBooting) {
                            // Loading bar: outline + fill
                            var barW = w * 0.52, barH = h * 0.17;
                            var bx = (w - barW) / 2, by = cy - barH / 2;
                            var line = Math.max(w * 0.05, 1.2);
                            ctx.lineWidth = line;
                            roundedRect(ctx, bx, by, barW, barH, barH / 2);
                            ctx.stroke();
                            var gap = line * 1.1;
                            var innerH = barH - gap * 2;
                            var innerW = Math.max((barW - gap * 2) * root.loadingFill(), innerH);
                            roundedRect(ctx, bx + gap, by + gap, innerW, innerH, innerH / 2);
                            ctx.fill();
                        } else if (root.asleep) {
                            // Asleep: closed eyes
                            ctx.lineWidth = Math.max(w * 0.06, 1.6);
                            ctx.lineCap = "round";
                            var sleepEyes = [w * 0.37, w * 0.63];
                            for (var si = 0; si < sleepEyes.length; si++) {
                                ctx.beginPath();
                                ctx.moveTo(sleepEyes[si] - w * 0.06, cy + h * 0.02);
                                ctx.lineTo(sleepEyes[si] + w * 0.06, cy + h * 0.02);
                                ctx.stroke();
                            }
                        } else if (root.isBusy) {
                            var spacing = w * 0.14;
                            var baseX = w * 0.50 - spacing;
                            for (var i = 0; i < 3; i++) {
                                var offset = Math.sin(root.phase - i * 0.7);
                                var y = cy - offset * (h * 0.045);
                                var r = w * (0.046 + 0.008 * Math.max(offset, 0));
                                ctx.beginPath();
                                ctx.arc(baseX + i * spacing, y, r, 0, Math.PI * 2);
                                ctx.fill();
                            }
                        } else if (root.appyState === "listening" || root.appyExpression === "happy" || root.appyExpression === "excited") {
                            if (root.appyExpression === "excited") {
                                ctx.fillStyle = "rgba(240, 128, 138, 0.48)";
                                ctx.beginPath();
                                ctx.arc(w * 0.27, cy + h * 0.09, w * 0.06, 0, Math.PI * 2);
                                ctx.arc(w * 0.73, cy + h * 0.09, w * 0.06, 0, Math.PI * 2);
                                ctx.fill();
                                ctx.fillStyle = "rgb(20, 20, 24)";
                            }
                            ctx.lineWidth = Math.max(w * 0.065, 1.8);
                            ctx.lineCap = "round";
                            var eyes = [w * 0.36, w * 0.64];
                            for (var j = 0; j < eyes.length; j++) {
                                var ex = eyes[j];
                                ctx.beginPath();
                                ctx.moveTo(ex - w * 0.07, cy + h * 0.03);
                                ctx.bezierCurveTo(
                                    ex - w * 0.02, cy - h * 0.07,
                                    ex + w * 0.02, cy - h * 0.07,
                                    ex + w * 0.07, cy + h * 0.03
                                );
                                ctx.stroke();
                            }
                        } else if (root.appyExpression === "surprised") {
                            ctx.beginPath();
                            ctx.arc(w * 0.37, cy, w * 0.068, 0, Math.PI * 2);
                            ctx.arc(w * 0.63, cy, w * 0.068, 0, Math.PI * 2);
                            ctx.fill();
                        } else if (root.appyExpression === "skeptical") {
                            roundedRect(ctx, w * 0.37 - w * 0.06, cy - h * 0.028, w * 0.12, h * 0.056, h * 0.028);
                            ctx.fill();
                            var rH = Math.max(h * 0.24 * root.blinkScale, h * 0.03);
                            roundedRect(ctx, w * 0.63 - w * 0.038, cy - rH * 0.5 - h * 0.02, w * 0.076, rH, w * 0.038);
                            ctx.fill();
                        } else if (root.appyExpression === "focused") {
                            var fH = Math.max(h * 0.14 * root.blinkScale, h * 0.03);
                            roundedRect(ctx, w * 0.375 - w * 0.048, cy - fH * 0.5, w * 0.096, fH, w * 0.035);
                            ctx.fill();
                            roundedRect(ctx, w * 0.625 - w * 0.048, cy - fH * 0.5, w * 0.096, fH, w * 0.035);
                            ctx.fill();
                        } else {
                            var eyeH = Math.max(h * 0.24 * root.blinkScale, h * 0.03);
                            var eyeW = w * 0.076;
                            var idleEyes = [w * 0.37, w * 0.63];
                            for (var k = 0; k < idleEyes.length; k++) {
                                var ix = idleEyes[k];
                                roundedRect(ctx, ix - eyeW * 0.5, cy - eyeH * 0.5, eyeW, eyeH, eyeW * 0.5);
                                ctx.fill();
                            }
                        }

                        ctx.restore();
                    }
                }

                Rectangle {
                    visible: root.connected && root.msgCount > 0
                    height: 18
                    implicitWidth: Math.max(18, countText.implicitWidth + 10)
                    radius: 9
                    color: "#2c2c2e"

                    Text {
                        id: countText
                        anchors.centerIn: parent
                        text: root.msgCount > 99 ? "99+" : String(root.msgCount)
                        color: "#ffffff"
                        font.family: "Inter"
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                    }
                }
            }

            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    if (root.connected) {
                        var xhr = new XMLHttpRequest();
                        xhr.open("POST", "http://127.0.0.1:8002/toggle");
                        xhr.send();
                    } else if (!root.launching) {
                        root.launch();
                    }
                }
            }
        }
    }
}
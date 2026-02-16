/**
 * QuietCool BLE control node — send commands to the fan.
 */

module.exports = function (RED) {
    function QuietCoolControlNode(config) {
        RED.nodes.createNode(this, config);
        const node = this;

        node.configNodeId = config.fan;
        node.action = config.action || "set_mode";
        node.actionValue = config.actionValue || "";

        const fanConfig = RED.nodes.getNode(node.configNodeId);
        if (!fanConfig) {
            node.status({ fill: "red", shape: "ring", text: "no config" });
            return;
        }

        node.updateNodeStatus = function (connected) {
            node.status(
                connected
                    ? { fill: "green", shape: "dot", text: "connected" }
                    : { fill: "red", shape: "ring", text: "disconnected" }
            );
        };

        fanConfig.registerUser(node);

        node.on("input", function (msg, send, done) {
            send =
                send ||
                function () {
                    node.send.apply(node, arguments);
                };
            done =
                done ||
                function (err) {
                    if (err) node.error(err, msg);
                };

            // Determine action from node config or msg.payload
            let action = node.action;
            let args = {};

            if (msg.payload && typeof msg.payload === "object") {
                if (msg.payload.action) action = msg.payload.action;
                if (msg.payload.args) args = msg.payload.args;
            }

            // Map user-friendly actions to bridge commands
            switch (action) {
                case "off":
                    args = { mode: "Idle" };
                    action = "set_mode";
                    break;
                case "smart":
                    args = { mode: "TH" };
                    action = "set_mode";
                    break;
                case "run_high":
                    args = { speed: "HIGH" };
                    action = "set_speed";
                    break;
                case "run_medium":
                    args = { speed: "MEDIUM" };
                    action = "set_speed";
                    break;
                case "run_low":
                    args = { speed: "LOW" };
                    action = "set_speed";
                    break;
                case "timer":
                    args.hours = args.hours || msg.payload.hours || 1;
                    args.minutes = args.minutes || msg.payload.minutes || 0;
                    args.speed = args.speed || msg.payload.speed || "HIGH";
                    action = "set_timer";
                    break;
                case "preset":
                    args.name =
                        args.name ||
                        node.actionValue ||
                        msg.payload.preset ||
                        msg.payload.name ||
                        "";
                    action = "set_preset";
                    break;
                case "set_mode":
                    args.mode =
                        args.mode ||
                        node.actionValue ||
                        msg.payload.mode ||
                        "Idle";
                    break;
                case "set_speed":
                    args.speed =
                        args.speed ||
                        node.actionValue ||
                        msg.payload.speed ||
                        "HIGH";
                    break;
                default:
                    break;
            }

            node.status({ fill: "blue", shape: "dot", text: action });

            fanConfig.sendBridgeCommand(action, args, function (response) {
                if (response.ok) {
                    msg.payload = response.data;
                    node.updateNodeStatus(fanConfig.connected);
                    send(msg);
                    done();
                } else {
                    node.status({
                        fill: "red",
                        shape: "ring",
                        text: response.error || "error",
                    });
                    done(new Error(response.error || "Command failed"));
                }
            });
        });

        node.on("close", function (done) {
            fanConfig.deregisterUser(node);
            done();
        });
    }

    RED.nodes.registerType("quietcool-control", QuietCoolControlNode);
};

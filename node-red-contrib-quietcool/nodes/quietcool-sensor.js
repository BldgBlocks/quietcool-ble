/**
 * QuietCool BLE sensor node — read sensor data from the fan.
 */

module.exports = function (RED) {
    function QuietCoolSensorNode(config) {
        RED.nodes.createNode(this, config);
        const node = this;

        node.configNodeId = config.fan;
        node.query = config.query || "get_state";
        node.pollInterval = (parseInt(config.pollInterval) || 0) * 1000;
        node.pollTimer = null;

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

        function doQuery(msg) {
            msg = msg || { payload: {} };
            const query = node.query;

            node.status({ fill: "blue", shape: "dot", text: query });

            fanConfig.sendBridgeCommand(query, {}, function (response) {
                if (response.ok) {
                    const data = response.data || {};
                    msg.payload = data;

                    // Add convenience fields for common queries
                    if (query === "get_state") {
                        msg.temperature =
                            data.temperature_f || (data.Temp_Sample || 0) / 10.0;
                        msg.humidity =
                            data.humidity_pct || data.Humidity_Sample || 0;
                        msg.mode = data.Mode;
                        msg.range = data.Range;
                    } else if (query === "get_status") {
                        msg.temperature = data.temperature_f;
                        msg.humidity = data.humidity;
                        msg.mode = data.mode;
                        msg.range = data.range;
                    }

                    node.updateNodeStatus(fanConfig.connected);
                    node.send(msg);
                } else {
                    node.status({
                        fill: "red",
                        shape: "ring",
                        text: response.error || "error",
                    });
                    node.error(response.error || "Query failed", msg);
                }
            });
        }

        // Input-triggered query
        node.on("input", function (msg) {
            doQuery(msg);
        });

        // Optional polling
        if (node.pollInterval > 0) {
            const startPolling = () => {
                if (fanConfig.connected) {
                    node.pollTimer = setInterval(
                        () => doQuery(),
                        node.pollInterval
                    );
                } else {
                    setTimeout(startPolling, 5000);
                }
            };
            setTimeout(startPolling, 3000);
        }

        node.on("close", function (done) {
            if (node.pollTimer) {
                clearInterval(node.pollTimer);
                node.pollTimer = null;
            }
            fanConfig.deregisterUser(node);
            done();
        });
    }

    RED.nodes.registerType("quietcool-sensor", QuietCoolSensorNode);
};

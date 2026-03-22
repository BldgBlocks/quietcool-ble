/**
 * QuietCool BLE sensor node — read sensor data from the fan.
 *
 * Two query modes:
 *   "get_state"  → msg.state  (mode, range, temperature, humidity)
 *   "get_status" → msg.state + msg.info + msg.version + msg.params
 *                   + msg.presets + msg.timer (if in Timer mode)
 *
 * No polling — wire an inject node to trigger reads as needed.
 */

module.exports = function (RED) {
    function QuietCoolSensorNode(config) {
        RED.nodes.createNode(this, config);
        const node = this;

        node.configNodeId = config.fan;
        node.query = config.query || "get_state";

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

        /**
         * Build msg.state from a get_state response.
         */
        function buildState(data) {
            return {
                mode: data.Mode,
                range: data.Range,
                temperature: data.temperature_f || (data.Temp_Sample || 0) / 10.0,
                humidity: data.humidity_pct || data.Humidity_Sample || 0,
                sensorState: data.SensorState,
            };
        }

        function doQuery(msg) {
            const query = node.query;

            node.status({ fill: "blue", shape: "dot", text: query });

            fanConfig.sendBridgeCommand(query, {}, function (response) {
                if (response.ok) {
                    const data = response.data || {};

                    if (query === "get_state") {
                        msg.topic = "state";
                        msg.state = buildState(data);
                    } else if (query === "get_status") {
                        msg.topic = "full";
                        msg.state = {
                            mode: data.mode === "TH" ? "Smart" : data.mode,
                            range: data.range,
                            temperature: data.temperature_f,
                            humidity: data.humidity,
                            sensorState: data.sensor_state,
                        };
                        msg.info = {
                            name: data.name,
                            model: data.model,
                            serial: data.serial,
                            connected: data.connected,
                        };
                        msg.version = {
                            firmware: data.firmware,
                            hardware: data.hw_version,
                        };
                        msg.params = {
                            fanType: data.fan_type,
                            thresholds: data.active_thresholds,
                        };
                        msg.presets = {
                            list: data.presets || [],
                            active: data.active_preset || null,
                        };
                        if (data.remain_hours != null) {
                            msg.timer = {
                                hours: data.remain_hours,
                                minutes: data.remain_minutes,
                                seconds: data.remain_seconds,
                            };
                        }
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

        node.on("input", function (msg) {
            doQuery(msg);
        });

        node.on("close", function (done) {
            fanConfig.deregisterUser(node);
            done();
        });
    }

    RED.nodes.registerType("quietcool-sensor", QuietCoolSensorNode);
};

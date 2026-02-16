/**
 * QuietCool BLE config node — manages the Python bridge process lifecycle.
 */

const { spawn } = require("child_process");
const path = require("path");
const readline = require("readline");

// Path to Python bridge (module scope — used by both config node and HTTP admin endpoints)
const pythonDir = path.join(__dirname, "..", "python");
const venvPython = path.join(pythonDir, ".venv", "bin", "python3");
const bridgeScript = path.join(pythonDir, "bridge.py");

module.exports = function (RED) {
    function QuietCoolConfigNode(config) {
        RED.nodes.createNode(this, config);
        const node = this;

        node.address = config.address;
        node.phoneId = config.phoneId;
        node.autoConnect = config.autoConnect !== false;
        node.bridge = null;
        node.bridgeReady = false;
        node.connected = false;
        node.pendingCallbacks = {};
        node.msgCounter = 0;
        node.users = new Set();

        node.startBridge = function () {
            if (node.bridge) return;

            node.log(`Starting BLE bridge for ${node.address}`);

            node.bridge = spawn(venvPython, [bridgeScript], {
                cwd: pythonDir,
                stdio: ["pipe", "pipe", "pipe"],
                env: {
                    ...process.env,
                    QUIETCOOL_LOG_LEVEL: "WARNING",
                },
            });

            // Read JSON responses from stdout
            const rl = readline.createInterface({ input: node.bridge.stdout });
            rl.on("line", (line) => {
                try {
                    const msg = JSON.parse(line);

                    if (msg.type === "status") {
                        node.connected = msg.connected;
                        node.bridgeReady = true;
                        node.updateStatus();

                        if (
                            msg.detail === "bridge_ready" &&
                            node.autoConnect &&
                            node.address &&
                            node.phoneId
                        ) {
                            node.sendBridgeCommand("connect", {
                                address: node.address,
                                phone_id: node.phoneId,
                            });
                        }
                        return;
                    }

                    // Route response to waiting callback
                    const cb = node.pendingCallbacks[msg.id];
                    if (cb) {
                        delete node.pendingCallbacks[msg.id];
                        cb(msg);
                    }
                } catch (e) {
                    node.warn(`Bridge parse error: ${e.message} - ${line}`);
                }
            });

            // Log bridge stderr
            const stderrRl = readline.createInterface({
                input: node.bridge.stderr,
            });
            stderrRl.on("line", (line) => {
                node.trace(`Bridge: ${line}`);
            });

            node.bridge.on("exit", (code, signal) => {
                node.warn(`Bridge exited: code=${code} signal=${signal}`);
                node.bridge = null;
                node.bridgeReady = false;
                node.connected = false;
                node.updateStatus();

                // Reject all pending callbacks
                for (const id of Object.keys(node.pendingCallbacks)) {
                    node.pendingCallbacks[id]({
                        ok: false,
                        error: "Bridge process exited",
                    });
                }
                node.pendingCallbacks = {};

                // Auto-restart after 5s if we still have users
                if (node.users.size > 0) {
                    setTimeout(() => node.startBridge(), 5000);
                }
            });

            node.bridge.on("error", (err) => {
                node.error(`Bridge spawn error: ${err.message}`);
            });
        };

        node.stopBridge = function () {
            if (node.bridge) {
                node.log("Stopping BLE bridge");
                node.bridge.kill("SIGTERM");
                node.bridge = null;
                node.bridgeReady = false;
                node.connected = false;
            }
        };

        node.sendBridgeCommand = function (cmd, args, callback) {
            if (!node.bridge || !node.bridgeReady) {
                if (callback) {
                    callback({ ok: false, error: "Bridge not ready" });
                }
                return;
            }

            const id = `msg_${++node.msgCounter}`;
            const msg = JSON.stringify({ id, cmd, args: args || {} }) + "\n";

            if (callback) {
                node.pendingCallbacks[id] = callback;
                // Timeout after 15s
                setTimeout(() => {
                    if (node.pendingCallbacks[id]) {
                        delete node.pendingCallbacks[id];
                        callback({ ok: false, error: "Command timeout" });
                    }
                }, 15000);
            }

            node.bridge.stdin.write(msg);
        };

        node.registerUser = function (userNode) {
            node.users.add(userNode.id);
            if (!node.bridge) {
                node.startBridge();
            }
        };

        node.deregisterUser = function (userNode) {
            node.users.delete(userNode.id);
            if (node.users.size === 0) {
                node.stopBridge();
            }
        };

        node.updateStatus = function () {
            for (const userId of node.users) {
                const userNode = RED.nodes.getNode(userId);
                if (userNode && userNode.updateNodeStatus) {
                    userNode.updateNodeStatus(node.connected);
                }
            }
        };

        node.on("close", function (done) {
            node.stopBridge();
            done();
        });
    }

    RED.nodes.registerType("quietcool-config", QuietCoolConfigNode);

    // ================================================================
    // HTTP Admin Endpoints for editor UI (scan, pair, generate ID)
    // ================================================================

    // Scan for QuietCool fans
    RED.httpAdmin.get(
        "/quietcool/scan",
        RED.auth.needsPermission("quietcool-config.write"),
        function (req, res) {
            const proc = spawn(venvPython, [bridgeScript], {
                cwd: pythonDir,
                stdio: ["pipe", "pipe", "pipe"],
            });

            let responded = false;
            const rl = readline.createInterface({ input: proc.stdout });

            rl.on("line", (line) => {
                try {
                    const msg = JSON.parse(line);
                    if (msg.type === "status" && msg.detail === "bridge_ready") {
                        proc.stdin.write(
                            JSON.stringify({ id: "scan", cmd: "scan", args: { timeout: 8 } }) + "\n"
                        );
                    } else if (msg.id === "scan" && !responded) {
                        responded = true;
                        res.json(msg.ok ? msg.data : { error: msg.error });
                        proc.kill("SIGTERM");
                    }
                } catch (e) {
                    /* ignore parse errors */
                }
            });

            proc.on("error", (err) => {
                if (!responded) {
                    responded = true;
                    res.status(500).json({ error: err.message });
                }
            });

            setTimeout(() => {
                if (!responded) {
                    responded = true;
                    res.status(504).json({ error: "Scan timeout" });
                    proc.kill("SIGTERM");
                }
            }, 15000);
        }
    );

    // Generate a new Phone ID
    RED.httpAdmin.get(
        "/quietcool/generate-id",
        RED.auth.needsPermission("quietcool-config.write"),
        function (req, res) {
            const crypto = require("crypto");
            const id = crypto.randomBytes(8).toString("hex");
            res.json({ phone_id: id });
        }
    );

    // Pair with a fan
    RED.httpAdmin.post(
        "/quietcool/pair",
        RED.auth.needsPermission("quietcool-config.write"),
        function (req, res) {
            const address = req.body.address;
            const phoneId = req.body.phoneId;

            if (!address || !phoneId) {
                res.status(400).json({ error: "address and phoneId required" });
                return;
            }

            const proc = spawn(venvPython, [bridgeScript], {
                cwd: pythonDir,
                stdio: ["pipe", "pipe", "pipe"],
            });

            let responded = false;
            const rl = readline.createInterface({ input: proc.stdout });

            rl.on("line", (line) => {
                try {
                    const msg = JSON.parse(line);
                    if (msg.type === "status" && msg.detail === "bridge_ready") {
                        proc.stdin.write(
                            JSON.stringify({
                                id: "pair",
                                cmd: "pair",
                                args: { address, phone_id: phoneId },
                            }) + "\n"
                        );
                    } else if (msg.id === "pair" && !responded) {
                        responded = true;
                        res.json(msg.ok ? msg.data : { error: msg.error });
                        proc.kill("SIGTERM");
                    }
                } catch (e) {
                    /* ignore */
                }
            });

            proc.on("error", (err) => {
                if (!responded) {
                    responded = true;
                    res.status(500).json({ error: err.message });
                }
            });

            setTimeout(() => {
                if (!responded) {
                    responded = true;
                    res.status(504).json({ error: "Pair timeout" });
                    proc.kill("SIGTERM");
                }
            }, 30000);
        }
    );
};

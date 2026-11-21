# NixOS VM test: payload activation, controller socket activation, host
# requirements and System Waydroid integration. Run with
#   nix build .#checks.x86_64-linux.vm -L
{ self, pkgs }:

pkgs.testers.runNixOSTest {
  name = "anvildroid";

  nodes.machine = {
    imports = [ self.nixosModules.default ];
    programs.anvildroid.enable = true;
    users.users.alice = {
      isNormalUser = true;
      uid = 1000;
    };
    virtualisation.memorySize = 2048;
  };

  testScript = ''
    import json

    machine.wait_for_unit("multi-user.target")

    with subtest("payload is a real root-owned copy"):
        machine.succeed("test -d /usr/local/lib/anvildroid-controller -a ! -L /usr/local/lib/anvildroid-controller")
        for path in ["/usr", "/usr/local", "/usr/local/lib", "/usr/local/lib/anvildroid-controller",
                     "/usr/local/lib/anvildroid-controller/runtime-controller.py"]:
            owner, mode = machine.succeed(f"stat -c '%u %a' {path}").split()
            assert owner == "0" and int(mode, 8) & 0o022 == 0, f"{path}: {owner} {mode}"
        machine.succeed("cmp ${self.packages.x86_64-linux.anvildroid}/lib/anvildroid-controller/runtime-worker.py /usr/local/lib/anvildroid-controller/runtime-worker.py")

    with subtest("payload manifests match the patched binaries"):
        machine.succeed("""
          cd /usr/local/lib/anvildroid-controller && /usr/bin/python3 -B -c '
        import importlib.util
        spec = importlib.util.spec_from_file_location("x11", "runtime-x11.py")
        x11 = importlib.util.module_from_spec(spec); spec.loader.exec_module(x11)
        x11.load_payload(x11.INSTALL)
        '
        """)

    with subtest("re-activation keeps runtime downloads"):
        machine.succeed("echo cached > /usr/local/lib/anvildroid-controller/libndk-0.2.3.zip")
        machine.succeed("rm /usr/local/lib/anvildroid-controller/.nix-source")
        machine.succeed("systemctl restart anvildroid-payload.service")
        machine.succeed("grep -q cached /usr/local/lib/anvildroid-controller/libndk-0.2.3.zip")
        machine.succeed("test -f /usr/local/lib/anvildroid-controller/runtime-controller.py")

    with subtest("shims"):
        machine.succeed("/usr/bin/python3 -c 'import gi, gbinder, dbus'")
        machine.succeed("/usr/bin/java -version")
        machine.succeed("test -x /usr/bin/waydroid -a -x /usr/bin/anvildroid-gui")

    with subtest("controller starts on the first client"):
        machine.wait_for_unit("anvildroid-controller.socket")
        out = machine.succeed("su alice -c 'anvildroid managed-runtimes --json'")
        print(out)
        machine.wait_for_unit("anvildroid-controller.service")
        reply = json.loads(machine.succeed(
            "su alice -c \"/usr/bin/python3 -c 'import json,socket;"
            "s=socket.socket(socket.AF_UNIX);s.connect(\\\"/run/anvildroid/control.sock\\\");"
            "s.sendall(json.dumps({\\\"op\\\":\\\"requirements\\\"}).encode()+b\\\"\\\\n\\\");"
            "print(s.makefile().readline())'\""))
        print(json.dumps(reply, indent=2))
        missing = [item for item in reply.get("result", reply).get("checks", reply.get("result", reply).get("requirements", []))
                   if isinstance(item, dict) and not item.get("available", True) and not item.get("optional")]
        print("missing:", missing)

    with subtest("System Waydroid runs through the AnvilDroid wrapper"):
        machine.succeed("systemctl cat waydroid-container.service | grep -q 'runtime-system.py'")
        print(machine.execute("systemctl status waydroid-container.service --no-pager; journalctl -u waydroid-container -b --no-pager | tail -30")[1])
  '';
}

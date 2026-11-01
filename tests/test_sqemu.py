import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src import cli, config, deps, disks, menu, qemu, usb, validate, vm_manager
from src.config import VM, Disk
from src.validate import SqemuError

ROOT = Path(__file__).resolve().parent.parent


def make_vm(**settings):
    defaults = {"name": "debian", "disks": [Disk("/vm/a.qcow2", "qcow2")], "mac": "52:54:00:aa:bb:cc"}
    return VM(**{**defaults, **settings})


def value_after(argv, flag):
    return [argv[i + 1] for i, item in enumerate(argv) if item == flag]


class TempData(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.tmp)})
        patcher.start()
        self.addCleanup(patcher.stop)


class ValidateTests(unittest.TestCase):
    def test_rejects_shell_and_option_injection_in_names(self):
        for bad in ("x; id #", "$(id)", "-rf", "../x", "a b", "", "a" * 65):
            with self.assertRaises(SqemuError, msg=bad):
                validate.name(bad)

    def test_sizes(self):
        self.assertEqual(validate.size("8g"), "8G")
        self.assertEqual(validate.size_to_bytes("1.5G"), 1610612736)
        for bad in ("8", "G", "-1G", "8G; id", "0G"):
            with self.assertRaises(SqemuError, msg=bad):
                validate.size(bad)

    def test_network(self):
        self.assertEqual(validate.network("tap:tap0"), "tap:tap0")
        self.assertEqual(validate.network("bridge:br0"), "bridge:br0")
        for bad in ("tap:", "tap:a,script=/bin/evil", "bridge", "bridge:", "bridge:br0,helper=/bin/evil", "ponte:br0"):
            with self.assertRaises(SqemuError, msg=bad):
                validate.network(bad)


class BuildArgvTests(unittest.TestCase):
    def test_disk_format_is_always_explicit(self):
        argv = qemu.build_argv(make_vm())
        self.assertIn("file=/vm/a.qcow2,format=qcow2,if=ide", value_after(argv, "-drive"))

    def test_commas_in_paths_cannot_add_options(self):
        argv = qemu.build_argv(make_vm(disks=[Disk("/vm/a,format=raw", "qcow2")]))
        self.assertIn("file=/vm/a,,format=raw,format=qcow2,if=ide", value_after(argv, "-drive"))

    def test_multiple_disks_and_live_iso(self):
        options = qemu.RunOptions(iso="/iso/d.iso", extra_disks=[Disk("/vm/b.img", "raw")])
        argv = qemu.build_argv(make_vm(), options)
        drives = value_after(argv, "-drive")
        self.assertEqual(len(drives), 3)
        self.assertTrue(any("media=cdrom" in drive for drive in drives))
        self.assertEqual(value_after(argv, "-boot"), ["once=d,menu=on"])

    def test_mac_is_stable_and_nic_has_a_backend(self):
        argv = qemu.build_argv(make_vm())
        self.assertEqual(value_after(argv, "-netdev"), ["user,id=net0"])
        self.assertIn("e1000,netdev=net0,mac=52:54:00:aa:bb:cc", value_after(argv, "-device"))

    def test_tap_and_no_network(self):
        tap = qemu.build_argv(make_vm(network="tap:tap0"))
        self.assertEqual(value_after(tap, "-netdev"), ["tap,id=net0,ifname=tap0,script=no,downscript=no"])
        self.assertEqual(value_after(qemu.build_argv(make_vm(network="none")), "-nic"), ["none"])

    def test_bridge(self):
        argv = qemu.build_argv(make_vm(network="bridge:br0"))
        self.assertEqual(value_after(argv, "-netdev"), ["bridge,id=net0,br=br0"])
        self.assertIn("e1000,netdev=net0,mac=52:54:00:aa:bb:cc", value_after(argv, "-device"))

    def test_audio_includes_a_sound_card(self):
        devices = value_after(qemu.build_argv(make_vm()), "-device")
        self.assertNotIn("intel-hda", devices)
        devices = value_after(qemu.build_argv(make_vm(audio="pa")), "-device")
        self.assertIn("intel-hda", devices)
        self.assertIn("hda-output,audiodev=audio0", devices)

    def test_usb_uses_a_usb_storage_device(self):
        argv = qemu.build_argv(make_vm(), qemu.RunOptions(usb_device="/dev/sdb"))
        self.assertIn("usb-storage,bus=usb.0,drive=usbdisk", value_after(argv, "-device"))
        self.assertIn("file=/dev/sdb,format=raw,if=none,id=usbdisk,cache=none", value_after(argv, "-drive"))

    def test_spice_is_local_only(self):
        argv = qemu.build_argv(make_vm(), qemu.RunOptions(spice_port=5901))
        self.assertEqual(value_after(argv, "-spice"), ["port=5901,addr=127.0.0.1,disable-ticketing=on"])
        self.assertEqual(value_after(argv, "-vga"), ["qxl"])

    def test_clipboard_channel_exists_only_with_spice(self):
        argv = qemu.build_argv(make_vm(), qemu.RunOptions(spice_port=5901))
        self.assertEqual(value_after(argv, "-chardev"), ["spicevmc,id=vdagent,name=vdagent"])
        self.assertIn("virtserialport,chardev=vdagent,name=com.redhat.spice.0", value_after(argv, "-device"))
        self.assertEqual(value_after(qemu.build_argv(make_vm()), "-chardev"), [])

    def test_serial_console_is_the_default(self):
        self.assertIn("-nographic", qemu.build_argv(make_vm()))
        self.assertNotIn("-nographic", qemu.build_argv(make_vm(display="window")))
        self.assertNotIn("-nographic", qemu.build_argv(make_vm(), qemu.RunOptions(display="window")))
        self.assertNotIn("-nographic", qemu.build_argv(make_vm(), qemu.RunOptions(spice_port=5900)))

    def test_overrides_do_not_change_the_vm(self):
        vm = make_vm()
        argv = qemu.build_argv(vm, qemu.RunOptions(ram="8G", cpus=4))
        self.assertEqual(value_after(argv, "-m"), ["8G"])
        self.assertEqual(value_after(argv, "-smp"), ["4"])
        self.assertEqual(vm.ram, "2G")


class ConfigTests(TempData):
    def test_round_trip_with_special_characters(self):
        vm = make_vm(disks=[Disk('/vm/perché "a" \\ b.img', "raw")], network="tap:tap0")
        config.save(vm)
        self.assertEqual(config.load("debian"), vm)
        self.assertEqual(config.list_names(), ["debian"])

    def test_invalid_file_is_reported_without_crashing(self):
        for content in ('name = "x"\nunknown = 1\n', "not toml", 'name = "$(id)"\n'):
            config.vm_dir("broken").mkdir(parents=True, exist_ok=True)
            (config.vm_dir("broken") / config.CONFIG_FILE).write_text(content)
            with self.assertRaises(SqemuError, msg=content):
                config.load("broken")

    def test_macs_are_random_and_valid(self):
        self.assertEqual(validate.mac(config.random_mac())[:9], "52:54:00:")
        self.assertGreater(len({config.random_mac() for _ in range(50)}), 40)


class DiskTests(TempData):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("src.disks.runner.run")
        self.run = patcher.start()
        self.addCleanup(patcher.stop)

    def _write_library_disk(self, filename="a.qcow2"):
        config.disks_dir().mkdir(parents=True, exist_ok=True)
        path = config.disks_dir() / filename
        path.touch()
        return path

    def test_create_uses_the_library_and_never_a_shell_string(self):
        path = disks.create("win", "8G")
        self.run.assert_called_once_with(["qemu-img", "create", "-f", "qcow2", path, "8G"])
        self.assertEqual(path, str(config.disks_dir() / "win.qcow2"))

    def test_create_refuses_to_overwrite(self):
        self._write_library_disk("win.qcow2")
        with self.assertRaises(SqemuError):
            disks.create("win", "8G")
        self.run.assert_not_called()

    def test_vmdk_conversion_uses_the_detected_format(self):
        """Regressione: la vecchia opzione 3 forzava raw anche sui dischi qcow2."""
        source = self._write_library_disk()
        with mock.patch("src.disks.detect_format", return_value="qcow2"):
            target = disks.convert("a", "out", "vmdk")
        self.run.assert_called_once()
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[:6], ["qemu-img", "convert", "-p", "-f", "qcow2", "-O"])
        self.assertEqual(argv[6:], ["vmdk", str(source), target])

    def test_dmg_falls_back_to_qemu_img(self):
        source = self.tmp / "mac.dmg"
        source.touch()
        with mock.patch("src.disks.shutil.which", return_value=None):
            disks.convert(str(source), "mac", "raw")
        self.assertIn("dmg", self.run.call_args.args[0])

    def test_dmg_uses_dmg2img_when_available(self):
        source = self.tmp / "mac.dmg"
        source.touch()
        with mock.patch("src.disks.shutil.which", return_value="/usr/bin/dmg2img"):
            disks.convert(str(source), "mac", "raw")
        self.assertEqual(self.run.call_args.args[0][0], "dmg2img")

    def test_shrinking_requires_confirmation(self):
        self._write_library_disk()
        details = {"format": "qcow2", "virtual-size": validate.size_to_bytes("10G")}
        with mock.patch("src.disks.info", return_value=details):
            with self.assertRaises(SqemuError):
                disks.resize("a", "5G")
            self.run.assert_not_called()
            disks.resize("a", "5G", shrink=True)
        self.assertIn("--shrink", self.run.call_args.args[0])

    def test_clone_keeps_the_format(self):
        self._write_library_disk()
        with mock.patch("src.disks.detect_format", return_value="qcow2"):
            target = disks.clone("a", "copy")
        self.assertTrue(target.endswith("copy.qcow2"))
        self.assertEqual(self.run.call_args.args[0][:2], ["qemu-img", "convert"])

    def test_paths_are_always_absolute(self):
        self.assertTrue(os.path.isabs(disks.resolve("./-rf.img")))
        self.assertTrue(os.path.isabs(disks.resolve("nbd:x/y")))


class UsbTests(unittest.TestCase):
    OUTPUT = json.dumps(
        {
            "blockdevices": [
                {"name": "sda", "path": "/dev/sda", "tran": "usb", "type": "disk", "size": 0, "model": "Lettore"},
                {"name": "nvme0n1", "path": "/dev/nvme0n1", "tran": "nvme", "type": "disk", "size": 500, "model": "SSD"},
                {
                    "name": "sdb", "path": "/dev/sdb", "tran": "usb", "type": "disk", "size": 8 * 1024**3,
                    "model": "Stick", "mountpoints": [None],
                },
                {
                    "name": "sdc", "path": "/dev/sdc", "tran": "usb", "type": "disk", "size": 16, "model": None,
                    "children": [{"name": "sdc1", "mountpoints": ["/run/media/x"]}],
                },
            ]
        }
    )

    def setUp(self):
        self.found = usb.parse_lsblk(self.OUTPUT)

    def test_empty_readers_and_internal_disks_are_excluded(self):
        self.assertEqual([disk.path for disk in self.found], ["/dev/sdb", "/dev/sdc"])

    def test_selection(self):
        self.assertEqual(usb.check_selection("sdb", self.found), "/dev/sdb")
        self.assertEqual(usb.check_selection("/dev/sdb", self.found), "/dev/sdb")

    def test_internal_and_mounted_disks_are_refused(self):
        for device in ("nvme0n1", "/dev/nvme0n1", "sda", "sdc", "sdb; id"):
            with self.assertRaises(SqemuError, msg=device):
                usb.check_selection(device, self.found)


class DepsTests(unittest.TestCase):
    def test_derivatives_map_to_the_right_family(self):
        cases = {
            'ID=cachyos\nID_LIKE=arch\n': "arch",
            'ID=ubuntu\nID_LIKE=debian\n': "debian",
            'ID=linuxmint\nID_LIKE="ubuntu debian"\n': "debian",
            'ID="rocky"\nID_LIKE="rhel centos fedora"\n': "fedora",
            'ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n': "suse",
        }
        for text, expected in cases.items():
            self.assertEqual(deps.family(deps.parse_os_release(text)), expected, text)

    def test_unknown_distribution(self):
        with self.assertRaises(SqemuError):
            deps.install_argv({"ID": "plan9"})

    def test_install_includes_audio_spice_and_clipboard(self):
        argv = deps.install_argv({"ID": "arch"})
        self.assertEqual(argv[:4], ["sudo", "pacman", "-S", "--needed"])
        for package in ("qemu-base", "qemu-audio-pipewire", "qemu-ui-spice-core", "qemu-chardev-spice", "virt-viewer"):
            self.assertIn(package, argv)

    def test_parse_backends_stops_at_the_blank_line(self):
        output = "Available display backend types:\nnone\n\nSome display backends support suboptions\n"
        self.assertEqual(deps.parse_backends(output), ["none"])

    def test_missing_features_give_clear_errors(self):
        with mock.patch("src.deps.runner.capture", return_value="Types:\nnone\n\n"):
            with self.assertRaises(SqemuError):
                deps.check_window()
            with self.assertRaises(SqemuError):
                deps.check_audio("pa")
            deps.check_audio("none")
        with mock.patch("src.deps.runner.capture", return_value='name "e1000", bus PCI\n'):
            with self.assertRaises(SqemuError):
                deps.check_spice()
        with mock.patch("src.deps.runner.capture", return_value='name "qxl-vga", bus PCI\n'), mock.patch(
            "src.deps.runner.probe", return_value="'spicevmc' is not a valid char driver name"
        ):
            with self.assertRaisesRegex(SqemuError, "qemu-chardev-spice"):
                deps.check_spice()


class AppTests(TempData):
    def test_start_requires_something_to_boot(self):
        config.save(make_vm(disks=[]))
        with self.assertRaises(SqemuError):
            vm_manager.prepare_start("debian")

    def test_missing_vm_and_iso(self):
        with self.assertRaises(SqemuError):
            vm_manager.prepare_start("missing")
        config.save(make_vm())
        with self.assertRaises(SqemuError):
            vm_manager.prepare_start("debian", iso=str(self.tmp / "missing.iso"))

    def test_missing_tap_interface_explains_how_to_create_it(self):
        config.save(make_vm(network="tap:nonexistent0"))
        with self.assertRaisesRegex(SqemuError, "ip tuntap add"):
            vm_manager.prepare_start("debian")

    def test_bridge_acl(self):
        allowed = vm_manager.bridge_allowed
        self.assertTrue(allowed("allow br0\n", "br0"))
        self.assertTrue(allowed("# commento\nallow all\n", "br0"))
        self.assertTrue(allowed("include /etc/qemu/altro.conf\n", "br0"))
        self.assertFalse(allowed("allow virbr0\n", "br0"))
        self.assertFalse(allowed("# allow br0\n", "br0"))
        self.assertFalse(allowed("deny br0\nallow all\n", "br0"))
        self.assertFalse(allowed("", "br0"))

    def test_bridge_problems_explain_the_fix(self):
        sys_net = self.tmp / "net"
        acl = self.tmp / "qemu/bridge.conf"
        helper = self.tmp / "helper"
        helper.touch()
        arguments = {"sys_net": str(sys_net), "acl_path": str(acl), "helpers": (str(helper),)}

        with self.assertRaisesRegex(SqemuError, "ip link add br0 type bridge"):
            vm_manager.check_bridge("br0", **arguments)
        (sys_net / "wlan0").mkdir(parents=True)
        with self.assertRaisesRegex(SqemuError, "non esiste"):
            vm_manager.check_bridge("wlan0", **arguments)

        (sys_net / "br0/bridge").mkdir(parents=True)
        with self.assertRaisesRegex(SqemuError, "allow br0"):
            vm_manager.check_bridge("br0", **arguments)

        acl.parent.mkdir()
        acl.write_text("allow br0\n")
        with self.assertRaisesRegex(SqemuError, "chmod u\\+s"):
            vm_manager.check_bridge("br0", **arguments)

        helper.chmod(0o4755)
        vm_manager.check_bridge("br0", **arguments)

    def test_invalid_settings_create_no_files(self):
        with self.assertRaises(SqemuError):
            vm_manager.create_vm("new", size="8G", network="bridge")
        self.assertFalse(config.vm_dir("new").exists())


class MenuTests(TempData):
    def _run(self, *answers):
        with mock.patch("builtins.input", side_effect=[*answers, "0"]), mock.patch("builtins.print") as printed:
            menu.run()
        return "\n".join(" ".join(map(str, call.args)) for call in printed.call_args_list)

    def test_bad_input_returns_to_the_menu(self):
        """Regressione: Invio, testo e numeri inesistenti causavano crash o uscite."""
        output = self._run("", "abc", '"a"', "3.5", "-1", "__import__('os').system('id')")
        self.assertEqual(output.count("Opzione non trovata."), 6)

    def test_errors_do_not_stop_the_menu(self):
        self.assertIn("Errore:", self._run("1", "x; id #", "8G"))

    def test_every_historic_option_exists(self):
        expected = {"1", "2", "3", "4", "4.5", "5", "6", "7", "8", "9", "10", "11", "12", "13"}
        self.assertEqual(set(menu.ACTIONS), expected)

    def test_end_of_input_exits_cleanly(self):
        with mock.patch("builtins.input", side_effect=EOFError), mock.patch("builtins.print"):
            menu.run()


class CliTests(TempData):
    def test_errors_become_exit_codes(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(cli.main(["start", "missing"]), 1)

    def test_launcher_works_from_another_directory_and_through_a_symlink(self):
        link = self.tmp / "sqemu64"
        link.symlink_to(ROOT / "sqemu64")
        result = subprocess.run([str(link), "--version"], cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name) / "casa con spazi"
        self.home.mkdir()
        self.env = {key: value for key, value in os.environ.items() if key != "XDG_DATA_HOME"}
        self.env.update(HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1")
        self.link = self.home / ".local/bin/sqemu64"
        self.icon = self.home / ".local/share/icons/hicolor/128x128/apps/sqemu64.png"
        self.desktop = self.home / ".local/share/applications/sqemu64.desktop"

    def _script(self, *args):
        return subprocess.run([ROOT / "install.sh", *args], env=self.env, capture_output=True, text=True)

    def test_install_and_uninstall(self):
        self.assertEqual(self._script().returncode, 0)
        self.assertEqual(self.icon.read_bytes(), (ROOT / "install_os/sqemu64.png").read_bytes())
        self.assertIn(f'Exec="{self.link}"', self.desktop.read_text().splitlines())
        result = subprocess.run([self.link, "--version"], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._script().returncode, 0, "deve poter essere rilanciato")

        vm = self.home / ".local/share/sqemu64/vms/a/vm.toml"
        vm.parent.mkdir(parents=True)
        vm.touch()
        self.assertEqual(self._script("--uninstall").returncode, 0)
        for path in (self.link, self.icon, self.desktop):
            self.assertFalse(path.exists(), path)
        self.assertTrue(vm.exists(), "VM e dischi non vanno toccati")

    def test_does_not_replace_foreign_files(self):
        self.link.parent.mkdir(parents=True)
        self.link.write_text("altro programma")
        self.assertNotEqual(self._script().returncode, 0)
        self.assertEqual(self._script("--uninstall").returncode, 0)
        self.assertEqual(self.link.read_text(), "altro programma")

        self.link.unlink()
        self.link.symlink_to("/usr/bin/true")
        self.assertNotEqual(self._script().returncode, 0)
        self.assertEqual(os.readlink(self.link), "/usr/bin/true")


def boot_sector(message):
    """Settore di avvio minimo che scrive un messaggio sulla porta seriale."""
    code = bytes.fromhex("fa31c08ed8baf803be177cfcac84c07403eeebf8f4ebfd") + message.encode() + b"\0"
    return code.ljust(510, b"\0") + b"\x55\xaa"


def boot_output(argv, marker, timeout=30):
    """Avvia QEMU, attende il messaggio e chiude la VM."""
    process = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    output = b""
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    try:
        while marker.encode() not in output:
            chunk = process.stdout.read1(4096)
            if not chunk:
                break
            output += chunk
    finally:
        timer.cancel()
        process.kill()
        process.wait()
        process.stdout.close()
    return output.decode(errors="replace")


@unittest.skipUnless(
    shutil.which("qemu-system-x86_64") and shutil.which("qemu-img") and os.access("/dev/kvm", os.W_OK),
    "QEMU o KVM non disponibili",
)
class RealBootTests(TempData):
    MARKER = "SQEMU-BOOT-OK"

    def _raw_disk(self):
        raw = self.tmp / "boot.img"
        raw.write_bytes(boot_sector(self.MARKER).ljust(1024 * 1024, b"\0"))
        return raw

    def _boots(self, value, **options):
        name = f"vm{len(config.list_names())}"
        vm_manager.create_vm(name, disk_values=[value])
        output = boot_output(vm_manager.prepare_start(name, **options), self.MARKER)
        self.assertIn(self.MARKER, output)

    def test_every_format_boots_after_conversion(self):
        """Regressione: il VMDK della vecchia opzione 3 non era avviabile."""
        raw = self._raw_disk()
        self._boots(str(raw))
        qcow2 = disks.convert(str(raw), "disk", "qcow2")
        self._boots(qcow2)
        self._boots(disks.convert(qcow2, "disk", "vmdk"))
        self._boots(disks.convert(qcow2, "disk2", "raw"))
        self._boots(disks.clone(qcow2, "copy"))
        disks.resize(qcow2, "2G")
        self._boots(qcow2)

    def test_full_options_are_accepted_by_qemu(self):
        raw = self._raw_disk()
        extra = disks.create("extra", "1M")
        self._boots(str(raw), disk_values=[extra], ram="256M", cpus=1)

    def test_chardev_probe_on_real_qemu(self):
        """Regressione: i moduli non sono elencati da '-chardev help' pur essendo disponibili."""
        self.assertTrue(deps.chardev_available("null"))
        self.assertFalse(deps.chardev_available("nonesiste"))

    @unittest.skipUnless(
        subprocess.run(["pacman", "-Q", "qemu-chardev-spice"], capture_output=True).returncode == 0
        if shutil.which("pacman") else False,
        "modulo SPICE non installato",
    )
    def test_spice_check_accepts_a_complete_installation(self):
        deps.check_spice()

    def test_clone_refuses_a_disk_in_use(self):
        raw = self._raw_disk()
        qcow2 = disks.convert(str(raw), "busy", "qcow2")
        vm_manager.create_vm("busy", disk_values=[qcow2])
        process = subprocess.Popen(
            vm_manager.prepare_start("busy"), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        try:
            process.stdout.read1(1)  # la VM ha aperto il disco e iniziato a scrivere
            with self.assertRaises(SqemuError):
                disks.clone(qcow2, "copy")
        finally:
            process.kill()
            process.wait()
            process.stdout.close()


@unittest.skipUnless(shutil.which("qemu-img"), "qemu-img non installato")
class RealQemuImgTests(TempData):
    def test_disk_lifecycle(self):
        path = disks.create("real", "1G")
        self.assertEqual(disks.detect_format(path), "qcow2")
        disks.resize("real", "2G")
        self.assertEqual(disks.info(path)["virtual-size"], 2 * 1024**3)
        with self.assertRaises(SqemuError):
            disks.resize("real", "1G")
        disks.resize("real", "1G", shrink=True)
        raw = disks.convert("real", "real-raw", "raw")
        self.assertEqual(disks.detect_format(raw), "raw")
        vmdk = disks.convert("real", "real-vmdk", "vmdk")
        self.assertEqual(disks.detect_format(vmdk), "vmdk")
        self.assertEqual(disks.detect_format(disks.clone("real", "copy")), "qcow2")


if __name__ == "__main__":
    unittest.main()

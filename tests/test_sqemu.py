import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src import cli, config, deps, disks, menu, network, qemu, usb, validate, vm_manager
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

    def test_saved_spice_uses_the_saved_port(self):
        argv = qemu.build_argv(make_vm(display="spice", spice_port=5902))
        self.assertEqual(value_after(argv, "-spice"), ["port=5902,addr=127.0.0.1,disable-ticketing=on"])
        self.assertNotIn("-nographic", argv)

    def test_a_single_start_can_override_the_saved_screen(self):
        vm = make_vm(display="spice")
        argv = qemu.build_argv(vm, qemu.RunOptions(display="serial"))
        self.assertIn("-nographic", argv)
        self.assertEqual(value_after(argv, "-spice"), [])

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

    def test_old_files_without_a_screen_stay_valid(self):
        config.vm_dir("old").mkdir(parents=True)
        (config.vm_dir("old") / config.CONFIG_FILE).write_text('name = "old"\ndisplay = "serial"\n')
        vm = config.load("old")
        self.assertEqual((vm.display, vm.spice_port), ("serial", 5900))
        config.vm_dir("older").mkdir(parents=True)
        (config.vm_dir("older") / config.CONFIG_FILE).write_text('name = "older"\n')
        self.assertIsNone(config.load("older").display)

    def test_unsaved_screen_is_not_written(self):
        self.assertNotIn("display", config.to_toml(make_vm()))
        self.assertEqual(config.from_toml(config.to_toml(make_vm())), make_vm())

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

    def test_spice_port_conflicts_with_another_screen(self):
        config.save(make_vm())
        with self.assertRaises(SqemuError):
            vm_manager.prepare_start("debian", spice_port="5900", display="serial")
        argv = vm_manager.prepare_start("debian", spice_port="5901", display="spice")
        self.assertEqual(value_after(argv, "-spice"), ["port=5901,addr=127.0.0.1,disable-ticketing=on"])

    def test_cli_options_do_not_change_the_saved_screen(self):
        config.save(make_vm(display="serial"))
        vm_manager.prepare_start("debian", spice_port="5901")
        self.assertEqual(config.load("debian").display, "serial")

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

    def _live_call(self, *screen_answers):
        config.save(make_vm())
        answers = ["7", "debian", "/iso/d.iso", "", "", "n", "", *screen_answers]
        with mock.patch("src.vm_manager.start") as start:
            output = self._run(*answers)
        return start, output

    def _start_disk_call(self, *screen_answers):
        with mock.patch("src.vm_manager.start") as start:
            output = self._run("5", "debian", "", "", "n", "", *screen_answers)
        return start, output

    def test_live_offers_spice_when_nothing_is_saved(self):
        start, _ = self._live_call("", "")
        self.assertEqual(start.call_args.kwargs["iso"], "/iso/d.iso")
        saved = config.load("debian")
        self.assertEqual((saved.display, saved.spice_port), ("spice", 5900))

    def test_normal_start_offers_serial_when_nothing_is_saved(self):
        config.save(make_vm())
        self._start_disk_call("")
        self.assertEqual(config.load("debian").display, "serial")

    def test_choice_is_saved_and_proposed_again(self):
        self._live_call("", "5905")
        start, _ = self._start_disk_call("", "")
        start.assert_called_once()
        saved = config.load("debian")
        self.assertEqual((saved.display, saved.spice_port), ("spice", 5905))
        argv = vm_manager.prepare_start("debian")
        self.assertEqual(value_after(argv, "-spice"), ["port=5905,addr=127.0.0.1,disable-ticketing=on"])

    def test_live_keeps_an_explicit_serial_choice(self):
        self._live_call("2")
        self.assertEqual(config.load("debian").display, "serial")
        self._live_call_keep()
        self.assertEqual(config.load("debian").display, "serial")

    def _live_call_keep(self):
        with mock.patch("src.vm_manager.start"):
            self._run("7", "debian", "/iso/d.iso", "", "", "n", "", "")

    def test_invalid_screen_saves_nothing(self):
        start, output = self._live_call("9")
        start.assert_not_called()
        self.assertIn("Scelta non valida", output)
        self.assertIsNone(config.load("debian").display)

    def test_invalid_port_saves_nothing(self):
        start, output = self._live_call("1", "80")
        start.assert_not_called()
        self.assertIn("Porta non valida", output)
        self.assertIsNone(config.load("debian").display)

    def _network_answers(self, *answers, screen=("2",)):
        config.save(make_vm())
        with mock.patch("src.vm_manager.start") as start, mock.patch("src.network.prepare_vm"), mock.patch(
            "src.vm_manager.list_interfaces", return_value=(["br0", "virbr0"], ["tap0"])
        ):
            output = self._run("5", "debian", "", "", "n", *answers, *screen)
        return start, output

    def test_network_choice_is_saved_and_proposed_again(self):
        start, output = self._network_answers("2", "")
        start.assert_called_once()
        self.assertIn("br0, virbr0", output)
        self.assertEqual(config.load("debian").network, "bridge:br0")
        with mock.patch("src.vm_manager.start"), mock.patch("src.network.prepare_vm"), mock.patch(
            "src.vm_manager.list_interfaces", return_value=(["br0", "virbr0"], [])
        ):
            self._run("5", "debian", "", "", "n", "", "", "2")
        self.assertEqual(config.load("debian").network, "bridge:br0")

    def test_network_other_choices(self):
        self._network_answers("3", "")
        self.assertEqual(config.load("debian").network, "tap:tap0")
        self._network_answers("4")
        self.assertEqual(config.load("debian").network, "none")
        self._network_answers("1")
        self.assertEqual(config.load("debian").network, "user")

    def test_invalid_network_saves_nothing(self):
        start, output = self._network_answers("9", screen=())
        start.assert_not_called()
        self.assertIn("Scelta non valida", output)
        self.assertEqual(config.load("debian").network, "user")
        start, _ = self._network_answers("2", "br0,helper=/bin/sh", screen=())
        start.assert_not_called()
        self.assertEqual(config.load("debian").network, "user")

    def test_wifi_bridge_option(self):
        with mock.patch("src.network.setup_wifi") as setup, mock.patch("src.network.remove_wifi") as remove:
            self._run("13", "", "", "192.168.1.50")
            self._run("13", "2", "br9")
        self.assertEqual(setup.call_args.args, ("br0", "192.168.1.50"))
        self.assertEqual(remove.call_args.args, ("br9",))

    def test_every_start_option_asks_for_the_screen(self):
        config.save(make_vm())
        config.save(make_vm(name="shell"))
        cases = {
            "5": ["5", "debian", "", "", "n", "", "2"],
            "6": ["6", "debian", "", "", "/d/b.img", "n", "", "2"],
            "7": ["7", "debian", "/iso/d.iso", "", "", "n", "", "2"],
            "8": ["8", "debian", "/d/b.img", "n", "/iso/d.iso", "", "", "", "2"],
            "9": ["9", "", "", "n", "", "2"],
        }
        for option, answers in cases.items():
            with self.subTest(option=option), mock.patch("src.vm_manager.start") as start:
                self._run(*answers)
                start.assert_called_once()

    def test_every_menu_option_exists(self):
        expected = {"1", "2", "3", "4", "4.5", "5", "6", "7", "8", "9", "10", "11", "12", "13"}
        self.assertEqual(set(menu.ACTIONS), expected)

    def test_menu_text_matches_the_actions(self):
        shown = set(re.findall(r"^\[([0-9.]+)\]", menu.MENU, re.MULTILINE))
        self.assertEqual(shown - {"0"}, set(menu.ACTIONS))
        self.assertNotIn("SPICE", menu.MENU)

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


def make_host(**settings):
    defaults = {
        "uplink": "wlan0",
        "address": ipaddress.IPv4Interface("192.168.1.31/24"),
        "gateway": ipaddress.IPv4Address("192.168.1.1"),
        "used_networks": [ipaddress.ip_network("192.168.1.0/24")],
    }
    return network.Host(**{**defaults, **settings})


def commands_of(steps):
    return [" ".join(step.argv) for step in steps]


class WifiBridgePlanTests(unittest.TestCase):
    IP = ipaddress.IPv4Address("192.168.1.50")

    def test_fresh_host_gets_every_step_with_its_undo(self):
        steps = network.plan("br0", self.IP, make_host())
        text = commands_of(steps)
        for expected in (
            "sudo ip link add br0 type bridge",
            "sudo ip addr add 192.168.77.1/24 dev br0",
            "sudo ip link set br0 up",
            "sudo tee -a /etc/qemu/bridge.conf",
            "sudo sysctl -w net.ipv4.ip_forward=1",
            "sudo sysctl -w net.ipv4.conf.wlan0.proxy_arp=1",
            "sudo sysctl -w net.ipv4.conf.br0.proxy_arp=1",
            "sudo ip route add 192.168.1.50/32 dev br0",
            "sudo iptables -I FORWARD -i br0 -j ACCEPT",
            "sudo iptables -I FORWARD -o br0 -j ACCEPT",
        ):
            self.assertIn(expected, text)
        self.assertIn(["sudo", "ip", "link", "del", "br0"], [step.undo for step in steps])
        for step in steps:
            self.assertEqual(step.argv[0], "sudo")
            self.assertIsNotNone(step.check if step.argv[1] == "iptables" else True)

    def test_existing_settings_are_left_alone(self):
        host = make_host(
            bridge_exists=True, bridge_has_address=True, acl_allowed=True, forward=1,
            proxy_arp={"wlan0": 1, "br0": 1}, route_exists=True,
        )
        steps = network.plan("br0", self.IP, host)
        undo = [step.undo for step in steps if step.undo]
        self.assertEqual(commands_of(steps)[0], "sudo ip link set br0 up")
        self.assertEqual(len(steps), 3)
        self.assertEqual([argv[2] for argv in undo], ["-D", "-D"])

    def test_undo_restores_previous_values(self):
        steps = network.plan("br0", self.IP, make_host(forward=0, proxy_arp={"wlan0": 0, "br0": 0}))
        undo = [" ".join(step.undo) for step in steps if step.undo]
        self.assertIn("sudo sysctl -w net.ipv4.ip_forward=0", undo)
        self.assertIn("sudo sed -i /^allow br0$/d /etc/qemu/bridge.conf", undo)

    def test_bridge_network_avoids_networks_in_use(self):
        used = [ipaddress.ip_network("192.168.77.0/24"), ipaddress.ip_network("192.168.1.0/24")]
        text = commands_of(network.plan("br0", self.IP, make_host(used_networks=used)))
        self.assertIn("sudo ip addr add 192.168.78.1/24 dev br0", text)

    def test_guest_ip_must_fit_the_host_network(self):
        host = make_host()
        self.assertEqual(str(network.parse_guest_ip("192.168.1.50", host)), "192.168.1.50")
        for bad in ("192.168.2.50", "192.168.1.31", "192.168.1.1", "192.168.1.0", "192.168.1.255", "x", "; id", ""):
            with self.assertRaises(SqemuError, msg=bad):
                network.parse_guest_ip(bad, host)


class WifiBridgeRunTests(TempData):
    IP = "192.168.1.50"

    def setUp(self):
        super().setUp()
        self.executed = []
        for target, replacement in (
            ("src.network.gather", lambda bridge, uplink=None: make_host()),
            ("src.network._route_elsewhere", lambda ip, bridge: False),
            ("src.network._route_exists", lambda ip, bridge: False),
            ("src.network._address_in_use", lambda ip: False),
            ("src.network._tool_exists", lambda tool: True),
            ("src.network._dnsmasq_running", lambda bridge: False),
            ("src.network.time.sleep", lambda seconds: None),
            ("src.network.runner.quiet_interactive", lambda argv: 0),
            ("src.network.runner.run", lambda argv, stdin_text=None: self.executed.append(argv)),
            ("src.network.runner.quiet", self._quiet),
        ):
            patcher = mock.patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.quiet_codes = {}

    def _quiet(self, argv):
        self.executed.append(argv)
        return self.quiet_codes.get(argv[2] if len(argv) > 2 else "", 1)

    def _setup(self, **arguments):
        with mock.patch("builtins.print"):
            return network.setup_wifi("br0", self.IP, **arguments)

    def _remove(self, **arguments):
        with mock.patch("builtins.print"):
            return network.remove_wifi("br0", **arguments)

    def test_declining_changes_nothing(self):
        self.assertFalse(self._setup(confirm=lambda: False))
        self.assertEqual(self.executed, [])
        self.assertFalse(network._state_path("br0").exists())

    def test_setup_then_remove_undoes_in_reverse_order(self):
        self.assertTrue(self._setup())
        added = [argv for argv in self.executed if argv[2] != "-C"]
        state = json.loads(network._state_path("br0").read_text())
        self.assertEqual(state["ips"], [self.IP])
        self.executed.clear()

        self.assertTrue(self._remove())
        self.assertEqual(self.executed[-1], ["sudo", "ip", "link", "del", "br0"])
        self.assertEqual(self.executed[0][:3], ["sudo", "iptables", "-D"])
        self.assertLess(len(self.executed), len(added))
        self.assertFalse(network._state_path("br0").exists())

    def test_existing_firewall_rule_is_not_added_or_removed(self):
        self.quiet_codes["-C"] = 0
        self._setup()
        self.assertFalse([argv for argv in self.executed if "-I" in argv])
        state = json.loads(network._state_path("br0").read_text())
        self.assertFalse([argv for argv in state["undo"] if "-D" in argv])

    def test_failure_keeps_what_is_needed_to_undo(self):
        def fail_on_route(argv, stdin_text=None):
            if "route" in argv:
                raise SqemuError("sudo è terminato con codice di errore 1.")

        with mock.patch("src.network.runner.run", fail_on_route):
            with self.assertRaisesRegex(SqemuError, "remove-wifi br0"):
                self._setup()
        state = json.loads(network._state_path("br0").read_text())
        self.assertIn(["sudo", "ip", "link", "del", "br0"], state["undo"])

    def test_second_vm_keeps_a_single_undo_for_shared_steps(self):
        self._setup()
        with mock.patch("builtins.print"):
            network.setup_wifi("br0", "192.168.1.51")
        state = json.loads(network._state_path("br0").read_text())
        self.assertEqual(state["ips"], ["192.168.1.50", "192.168.1.51"])
        self.assertEqual(len(state["undo"]), len({tuple(argv) for argv in state["undo"]}))
        self.assertEqual(sum("route" in argv for argv in state["undo"]), 2)

    def test_address_already_in_use_is_refused(self):
        with mock.patch("src.network._address_in_use", lambda ip: True):
            with self.assertRaisesRegex(SqemuError, "risponde già"):
                self._setup()
        self.assertEqual(self.executed, [])

    def test_remove_without_state_touches_nothing(self):
        with self.assertRaisesRegex(SqemuError, "nulla da annullare"):
            self._remove()
        self.assertEqual(self.executed, [])

    def test_tampered_state_is_not_executed(self):
        path = network._state_path("br0")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"undo": [["sudo", "sh", "-c", "id"]]}))
        with self.assertRaisesRegex(SqemuError, "non riconosciuto"):
            self._remove()
        self.assertEqual(self.executed, [])

    def test_invalid_names_are_rejected(self):
        for bad in ("br0;id", "a" * 16):
            with self.assertRaises(SqemuError, msg=bad):
                network.setup_wifi(bad, self.IP)


class WifiBridgeDhcpPlanTests(unittest.TestCase):
    IP = ipaddress.IPv4Address("192.168.1.50")
    HOSTS = {"52:54:00:aa:bb:cc": "192.168.1.50"}

    def test_dnsmasq_serves_only_the_vm_and_no_dns(self):
        argv = network.dnsmasq_argv("br0", make_host(), self.HOSTS)
        self.assertEqual(argv[:2], ["sudo", "dnsmasq"])
        for expected in (
            "--port=0",
            "--interface=br0",
            "--bind-interfaces",
            "--shared-network=br0,192.168.1.0",
            "--dhcp-range=192.168.1.0,static,255.255.255.0",
            "--dhcp-option=3,192.168.1.1",
            "--dhcp-option=6,192.168.1.1",
            "--dhcp-host=52:54:00:aa:bb:cc,192.168.1.50,infinite",
        ):
            self.assertIn(expected, argv)

    def test_plan_opens_the_dhcp_port_and_starts_dnsmasq(self):
        steps = network.plan("br0", self.IP, make_host(), self.HOSTS)
        text = commands_of(steps)
        self.assertIn("sudo iptables -I INPUT -i br0 -p udp --dport 67 -j ACCEPT", text)
        start = next(step for step in steps if step.argv[1] == "dnsmasq")
        self.assertEqual(start.undo, ["sudo", "pkill", "-F", "/run/sqemu64-dnsmasq-br0.pid"])

    def test_running_dnsmasq_is_kept_or_restarted_only_when_needed(self):
        running = make_host(dnsmasq_running=True)
        self.assertFalse([s for s in network.plan("br0", self.IP, running, self.HOSTS) if s.argv[1] in ("dnsmasq", "pkill")])
        tools = [s.argv[1] for s in network.plan("br0", self.IP, running, self.HOSTS, restart_dhcp=True)]
        self.assertLess(tools.index("pkill"), tools.index("dnsmasq"))

    def test_no_vm_means_no_dhcp(self):
        tools = [s.argv[1] for s in network.plan("br0", self.IP, make_host())]
        self.assertNotIn("dnsmasq", tools)


class WifiBridgeVmTests(TempData):
    def setUp(self):
        super().setUp()
        self.vm = make_vm(network="bridge:br0")
        config.save(self.vm)
        self.running = False
        self.executed = []
        self.in_use = set()
        self.routes = set()
        for target, replacement in (
            ("src.network.gather", lambda bridge, uplink=None: make_host(dnsmasq_running=self.running)),
            ("src.network._route_elsewhere", lambda ip, bridge: False),
            ("src.network._route_exists", lambda ip, bridge: str(ip) in self.routes),
            ("src.network._address_in_use", lambda ip: str(ip) in self.in_use),
            ("src.network._tool_exists", lambda tool: True),
            ("src.network._dnsmasq_running", lambda bridge: self.running),
            ("src.network.time.sleep", lambda seconds: None),
            ("src.network.runner.quiet_interactive", lambda argv: 0),
            ("src.network.runner.run", self._run_step),
            ("src.network.runner.quiet", lambda argv: 1),
        ):
            patcher = mock.patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run_step(self, argv, stdin_text=None):
        self.executed.append(argv)
        if argv[1] == "dnsmasq":
            self.running = True

    def _quiet_setup(self, ip="192.168.1.50", vm=None):
        with mock.patch("builtins.print"):
            return network.setup_wifi("br0", ip, vm=vm or self.vm)

    def test_setup_with_a_vm_starts_dhcp_and_remembers_it(self):
        self._quiet_setup()
        state = json.loads(network._state_path("br0").read_text())
        self.assertEqual(state["hosts"]["debian"], {"mac": self.vm.mac, "ip": "192.168.1.50"})
        self.assertEqual(state["dhcp_hosts"], {self.vm.mac: "192.168.1.50"})
        self.assertIn(["sudo", "pkill", "-F", "/run/sqemu64-dnsmasq-br0.pid"], state["undo"])

    def test_same_vm_again_does_not_restart_dhcp(self):
        self._quiet_setup()
        self.executed.clear()
        self._quiet_setup()
        self.assertFalse([argv for argv in self.executed if argv[1] in ("dnsmasq", "pkill")])

    def test_second_vm_restarts_dhcp_with_both_hosts(self):
        self._quiet_setup()
        self.executed.clear()
        other = make_vm(name="altra", mac="52:54:00:00:00:02", network="bridge:br0")
        self._quiet_setup("192.168.1.51", other)
        start = next(argv for argv in self.executed if argv[1] == "dnsmasq")
        self.assertIn(f"--dhcp-host={self.vm.mac},192.168.1.50,infinite", start)
        self.assertIn("--dhcp-host=52:54:00:00:00:02,192.168.1.51,infinite", start)
        self.assertIn("pkill", [argv[1] for argv in self.executed])

    def test_changing_the_ip_releases_the_old_address(self):
        """Regressione: la vecchia route faceva rispondere il computer per un IP non più usato."""
        self._quiet_setup("192.168.1.50")
        self.routes = {"192.168.1.50"}
        self.executed.clear()
        self._quiet_setup("192.168.1.60")

        self.assertIn(["sudo", "ip", "route", "del", "192.168.1.50/32", "dev", "br0"], self.executed)
        self.assertIn(["sudo", "ip", "route", "add", "192.168.1.60/32", "dev", "br0"], self.executed)
        start = next(argv for argv in self.executed if argv[1] == "dnsmasq")
        self.assertIn(f"--dhcp-host={self.vm.mac},192.168.1.60,infinite", start)
        self.assertNotIn(f"--dhcp-host={self.vm.mac},192.168.1.50,infinite", start)

        state = json.loads(network._state_path("br0").read_text())
        self.assertEqual(state["ips"], ["192.168.1.60"])
        self.assertEqual(state["hosts"]["debian"]["ip"], "192.168.1.60")
        routes = [argv for argv in state["undo"] if "route" in argv]
        self.assertEqual(routes, [["sudo", "ip", "route", "del", "192.168.1.60/32", "dev", "br0"]])

    def test_an_ip_of_another_vm_is_refused(self):
        self._quiet_setup()
        other = make_vm(name="altra", mac="52:54:00:00:00:02")
        with self.assertRaisesRegex(SqemuError, "già assegnato alla VM debian"):
            self._quiet_setup(vm=other)

    def test_proposal_starts_at_fifty_and_skips_addresses_in_use(self):
        host = make_host()
        self.assertEqual(network.propose_ip("br0", self.vm, host), "192.168.1.50")
        self.in_use = {"192.168.1.50", "192.168.1.51"}
        self.assertEqual(network.propose_ip("br0", self.vm, host), "192.168.1.52")

    def test_proposal_reuses_the_vm_address_after_a_reboot(self):
        self._quiet_setup("192.168.1.77")
        self.assertEqual(network.propose_ip("br0", self.vm, make_host()), "192.168.1.77")
        self.in_use = {"192.168.1.77"}
        self.assertEqual(network.propose_ip("br0", self.vm, make_host()), "192.168.1.50")

    def test_proposal_skips_addresses_routed_to_another_interface(self):
        """Regressione: proponeva un IP che setup_wifi avrebbe poi rifiutato."""
        with mock.patch("src.network._route_elsewhere", lambda ip, bridge: str(ip) == "192.168.1.50"):
            self.assertEqual(network.propose_ip("br0", self.vm, make_host()), "192.168.1.51")

    def test_proposal_in_a_small_network(self):
        host = make_host(address=ipaddress.IPv4Interface("10.0.0.2/29"), gateway=ipaddress.IPv4Address("10.0.0.1"))
        self.assertEqual(network.propose_ip("br0", self.vm, host), "10.0.0.3")

    def test_prepare_ignores_other_networks(self):
        config.save(make_vm())
        with mock.patch("src.network.setup_wifi") as setup:
            network.prepare_vm("debian")
        setup.assert_not_called()

    def test_prepare_leaves_foreign_bridges_alone(self):
        with mock.patch("src.network.setup_wifi") as setup, mock.patch("src.network._is_managed", return_value=False):
            network.prepare_vm("debian")
        setup.assert_not_called()

    def test_prepare_proposes_setup_when_not_ready(self):
        with mock.patch("src.network._is_managed", return_value=True), mock.patch("builtins.print"):
            network.prepare_vm("debian")
        self.assertTrue(json.loads(network._state_path("br0").read_text())["hosts"])

    def test_prepare_does_nothing_when_ready(self):
        with mock.patch("src.network._is_managed", return_value=True), mock.patch(
            "src.network.is_ready", return_value=True
        ), mock.patch("src.network.setup_wifi") as setup:
            network.prepare_vm("debian")
        setup.assert_not_called()

    def test_declining_stops_the_start(self):
        with mock.patch("src.network._is_managed", return_value=True), mock.patch("builtins.print"):
            with self.assertRaisesRegex(SqemuError, "Avvio annullato"):
                network.prepare_vm("debian", confirm=lambda: False)
        self.assertEqual(self.executed, [])

    def test_start_override_is_used_for_preparation(self):
        config.save(make_vm())
        with mock.patch("src.network._is_managed", return_value=True), mock.patch("builtins.print"):
            network.prepare_vm("debian", "bridge:br7")
        self.assertTrue(network._state_path("br7").exists())
        self.assertEqual(config.load("debian").network, "user")

    def _prepare_all(self, bridge=None):
        with mock.patch("src.network._is_managed", return_value=True), mock.patch("builtins.print") as printed:
            network.prepare_all(bridge)
        return " ".join(str(call.args[0]) for call in printed.call_args_list if call.args)

    def test_prepare_all_assigns_each_vm_its_own_address(self):
        config.save(make_vm(name="altra", mac="52:54:00:00:00:02", network="bridge:br0"))
        config.save(make_vm(name="internet", mac="52:54:00:00:00:03"))
        self._prepare_all()
        hosts = json.loads(network._state_path("br0").read_text())["hosts"]
        self.assertEqual(set(hosts), {"altra", "debian"})
        self.assertEqual(len({assigned["ip"] for assigned in hosts.values()}), 2)

    def test_prepare_all_keeps_saved_addresses(self):
        self._quiet_setup("192.168.1.77")
        self.routes = set()
        self.running = False
        self._prepare_all("br0")
        self.assertEqual(json.loads(network._state_path("br0").read_text())["hosts"]["debian"]["ip"], "192.168.1.77")

    def test_prepare_all_skips_ready_vms(self):
        with mock.patch("src.network.is_ready", return_value=True):
            self._quiet_setup()
            self.executed.clear()
            output = self._prepare_all()
        self.assertEqual(self.executed, [])
        self.assertIn("già pronta", output)

    def test_prepare_all_filters_by_bridge(self):
        config.save(make_vm(name="altra", mac="52:54:00:00:00:02", network="bridge:br1"))
        self._prepare_all("br1")
        self.assertTrue(network._state_path("br1").exists())
        self.assertFalse(network._state_path("br0").exists())

    def test_prepare_all_without_vms_explains_it(self):
        config.save(make_vm())
        with self.assertRaisesRegex(SqemuError, "Nessuna VM"):
            network.prepare_all()

    def test_prepare_all_leaves_foreign_bridges_alone(self):
        with mock.patch("src.network._is_managed", return_value=False), mock.patch("builtins.print"):
            network.prepare_all()
        self.assertEqual(self.executed, [])

    def test_ready_needs_everything_that_a_reboot_removes(self):
        self.assertFalse(network.is_ready("br0", self.vm))

    def test_menu_prepares_before_starting(self):
        config.save(make_vm())
        calls = []
        with mock.patch("src.network.prepare_vm", side_effect=lambda *a, **k: calls.append("prepare")), mock.patch(
            "src.vm_manager.start", side_effect=lambda *a, **k: calls.append("start")
        ), mock.patch("builtins.input", side_effect=["5", "debian", "", "", "n", "", "2", "0"]), mock.patch("builtins.print"):
            menu.run()
        self.assertEqual(calls, ["prepare", "start"])

    def test_cli_dry_run_never_prepares(self):
        config.save(make_vm(network="bridge:br0"))
        with mock.patch("src.network.prepare_vm") as prepare, mock.patch("src.vm_manager.start"):
            self.assertEqual(cli.main(["start", "debian", "--dry-run"]), 0)
            prepare.assert_not_called()
            self.assertEqual(cli.main(["start", "debian"]), 0)
            prepare.assert_called_once()


class StartNetworkOverrideTests(TempData):
    def test_network_for_a_single_start(self):
        config.save(make_vm())
        argv = vm_manager.prepare_start("debian", network="none")
        self.assertEqual(value_after(argv, "-nic"), ["none"])
        self.assertEqual(config.load("debian").network, "user")
        with self.assertRaises(SqemuError):
            vm_manager.prepare_start("debian", network="ponte")

    def test_list_interfaces(self):
        sys_net = self.tmp / "net"
        (sys_net / "br0/bridge").mkdir(parents=True)
        (sys_net / "tap0").mkdir()
        (sys_net / "tap0/tun_flags").touch()
        (sys_net / "wlan0").mkdir()
        self.assertEqual(vm_manager.list_interfaces(str(sys_net)), (["br0"], ["tap0"]))


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

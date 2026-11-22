import json
import logging
import re
import sys
import traceback

import pynetbox

from netbox_agent.config import config
from netbox_agent.config import netbox_instance as nb
from netbox_agent.lshw import LSHW
from netbox_agent.misc import get_vendor, is_tool
from netbox_agent.raid.hp import HPRaid
from netbox_agent.raid.omreport import OmreportRaid
from netbox_agent.raid.storcli import StorcliRaid

INVENTORY_TAG = {
    "cpu": {"name": "hw:cpu", "slug": "hw-cpu"},
    "gpu": {"name": "hw:gpu", "slug": "hw-gpu"},
    "disk": {"name": "hw:disk", "slug": "hw-disk"},
    "interface": {"name": "hw:interface", "slug": "hw-interface"},
    "memory": {"name": "hw:memory", "slug": "hw-memory"},
    "motherboard": {"name": "hw:motherboard", "slug": "hw-motherboard"},
    "raid_card": {"name": "hw:raid_card", "slug": "hw-raid-card"},
}


def natural_bay_sort_key(bay):
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", str(bay.name))
        if part
    )


class Inventory:
    """
    Hardware inventory backed by NetBox modules (module bays + module types)
    instead of the deprecated inventory items.

    This class implements for:
    * memory
    * cpu
    * raid cards
    * disks
    * gpus
    * motherboard
    * built-in interfaces

    methods that:
    * get local item
    * get netbox item
    * create netbox item
    * update netbox item

    Known issues:
    - no scan of non-raid devices
    - no scan of NVMe devices
    """

    def __init__(self, server, update_expansion=False):
        self.create_netbox_tags()
        self.server = server
        self.update_expansion = update_expansion
        netbox_server = self.server.get_netbox_server(update_expansion)

        self.device_id = netbox_server.id if netbox_server else None
        self.raid = None
        self.disks = []

        self.lshw = LSHW()

    def create_netbox_tags(self):
        ret = []
        for key, tag in INVENTORY_TAG.items():
            nb_tag = nb.extras.tags.get(name=tag["name"])
            if not nb_tag:
                nb_tag = nb.extras.tags.create(
                    name=tag["name"],
                    slug=tag["slug"],
                    comments=tag["name"],
                )
            ret.append(nb_tag)
        return ret

    def find_or_create_manufacturer(self, name):
        if name is None:
            return None

        manufacturer = nb.dcim.manufacturers.get(
            name=name,
        )
        if not manufacturer:
            logging.info(f"Creating missing manufacturer {name}")
            manufacturer = nb.dcim.manufacturers.create(
                name=name,
                slug=re.sub("[^A-Za-z0-9]+", "-", name).lower(),
            )

            logging.info(f"Creating missing manufacturer {name}")

        return manufacturer

    def get_netbox_inventory(self, device_id, tag):
        try:
            items = nb.dcim.inventory_items.filter(device_id=device_id, tag=tag)
        except pynetbox.core.query.RequestError:
            logging.info(f"Tag {tag} is missing, returning empty array.")
            items = []

        return list(items)

    def _normalize_serial(self, serial):
        if serial is None:
            return ""
        serial = str(serial).strip()
        return "" if serial in ("N/A", "No SN", "None") else serial

    def _get_netbox_modules(self, tag):
        try:
            mods = nb.dcim.modules.filter(device_id=self.device_id, tag=tag)
        except pynetbox.core.query.RequestError:
            logging.info(f"Tag {tag} is missing, returning empty array.")
            mods = []
        return list(mods)

    def _module_type_for(self, vendor, model, part=None, description=""):
        manufacturer = self.find_or_create_manufacturer(vendor)
        return self.find_or_create_module_type(
            manufacturer=manufacturer,
            model=str(model),
            part_number=part or "",
            description=description,
        )

    def _ensure_module_bay(self, name):
        bay = nb.dcim.module_bays.get(device_id=self.device_id, name=name)
        if bay:
            return bay
        logging.info(f"Creating ModuleBay {name} on device {self.device_id}")
        return nb.dcim.module_bays.create(device=self.device_id, name=name)

    def _sync_modules(self, kind, slots):
        slug = INVENTORY_TAG[kind]["slug"]
        nb_modules = self._get_netbox_modules(slug)
        type_cache = {}

        def mod_type_for(slot):
            key = (str(slot.get("vendor") or ""), str(slot["model"]))
            if key not in type_cache:
                type_cache[key] = self._module_type_for(
                    vendor=slot.get("vendor"),
                    model=slot["model"],
                    part=slot.get("part"),
                    description=slot.get("description", ""),
                )
            return type_cache[key]

        for mod in list(nb_modules):
            bay_name = mod.module_bay.name if mod.module_bay else None
            slot = slots.get(bay_name) if bay_name else None
            if slot is None:
                logging.info(f"Deleting unknown locally {kind} module {mod}")
                mod.delete()
                nb_modules.remove(mod)
                continue
            local_serial = self._normalize_serial(slot["serial"])
            same_serial = not (mod.serial and local_serial) or mod.serial == local_serial
            if not same_serial or mod.module_type.display != slot["model"]:
                logging.info(f"Replacing {kind} module {mod} in bay {bay_name}")
                mod.delete()
                nb_modules.remove(mod)

        for bay_name, slot in slots.items():
            existing = next((m for m in nb_modules if m.module_bay.name == bay_name), None)
            mod_type = mod_type_for(slot)
            if existing is not None:
                if existing.module_type.id != mod_type.id:
                    existing.module_type = mod_type.id
                    existing.save()
                continue
            bay = self._ensure_module_bay(bay_name)
            if getattr(bay, "installed_module", None):
                logging.warning(f"Bay {bay_name} is occupied, skipping {kind} module")
                continue
            serial = self._normalize_serial(slot["serial"])
            if serial and len(
                list(nb.dcim.modules.filter(device_id=self.device_id, serial=serial))
            ):
                logging.info(f"{kind} module with serial {serial} already exists")
                continue
            nb.dcim.modules.create(
                device=self.device_id,
                module_bay=bay.id,
                module_type=mod_type.id,
                serial=serial,
                status="active",
                description=slot.get("description", ""),
                tags=[{"name": INVENTORY_TAG[kind]["name"]}],
            )
            logging.info(
                f"Creating {kind} module {slot['model']} ({serial or 'no SN'}) in bay {bay_name}"
            )

    def purge_old_inventory_items(self):
        for tag in INVENTORY_TAG.values():
            items = self.get_netbox_inventory(device_id=self.device_id, tag=tag["slug"])
            for item in items:
                logging.info(f"Deleting old inventory item {item.name}")
                item.delete()

    def get_hw_motherboards(self):
        motherboards = []

        m = {}
        m["serial"] = self.lshw.motherboard_serial
        m["vendor"] = self.lshw.vendor
        m["name"] = f"{self.lshw.vendor} {self.lshw.motherboard}"
        m["description"] = f"{self.lshw.motherboard} Motherboard"

        motherboards.append(m)

        return motherboards

    def _slots_for_motherboard(self):
        motherboards = self.get_hw_motherboards()
        if not motherboards:
            return {}
        m = motherboards[0]
        model = self.lshw.motherboard or m.get("name") or "Motherboard"
        return {
            "Motherboard": {
                "vendor": m.get("vendor") or "",
                "model": str(model),
                "part": "",
                "serial": m.get("serial") or "",
                "description": m.get("description") or "Motherboard",
            }
        }

    def do_netbox_motherboard(self):
        self._sync_modules("motherboard", self._slots_for_motherboard())

    def _slots_for_interfaces(self):
        slots = {}
        for i, iface in enumerate(
            sorted(self.lshw.interfaces, key=lambda x: str(x.get("name", ""))), start=1
        ):
            bay_name = f"NIC {i}"
            description = "{} {}".format(
                iface.get("description", ""), iface.get("name", "")
            ).strip()
            slots[bay_name] = {
                "vendor": iface.get("vendor") or "Unknown",
                "model": iface.get("product") or "Unknown NIC",
                "part": "",
                "serial": self._normalize_serial(iface.get("serial", "")),
                "description": description,
            }
        return slots

    def do_netbox_interfaces(self):
        self._sync_modules("interface", self._slots_for_interfaces())

    def _slots_for_cpus(self):
        slots = {}
        cpus = self.lshw.get_hw_linux("cpu")
        for i, cpu in enumerate(cpus, start=1):
            bay_name = str(cpu.get("location") or f"CPU {i}")
            slots[bay_name] = {
                "vendor": cpu.get("vendor") or "Unknown vendor",
                "model": cpu.get("product") or f"CPU {i}",
                "part": "",
                "serial": cpu.get("serial", ""),
                "description": cpu.get("description") or "CPU",
            }
        return slots

    def do_netbox_cpus(self):
        self._sync_modules("cpu", self._slots_for_cpus())

    def get_raid_cards(self, filter_cards=False):
        raid_class = None
        if self.server.manufacturer in ("Dell", "Huawei"):
            if is_tool("omreport"):
                raid_class = OmreportRaid
            if is_tool("storcli"):
                raid_class = StorcliRaid
        elif self.server.manufacturer in ("HP", "HPE"):
            if is_tool("ssacli"):
                raid_class = HPRaid

        if not raid_class:
            return []

        self.raid = raid_class()

        if filter_cards and config.expansion_as_device and self.server.own_expansion_slot():
            return [
                c for c in self.raid.get_controllers() if c.is_external() is self.update_expansion
            ]
        else:
            return self.raid.get_controllers()

    def _slots_for_raid_cards(self):
        slots = {}
        seen = set()
        for i, raid_card in enumerate(self.get_raid_cards(filter_cards=True), start=1):
            model = raid_card.get_product_name() or "RAID Card"
            serial = self._normalize_serial(raid_card.get_serial_number())
            if serial in seen:
                serial = ""
            seen.add(serial)
            slots[f"RAID {i}"] = {
                "vendor": raid_card.get_manufacturer() or "",
                "model": str(model),
                "part": "",
                "serial": serial,
                "description": "RAID Card",
            }
        return slots

    def do_netbox_raid_cards(self):
        self._sync_modules("raid_card", self._slots_for_raid_cards())

    def is_virtual_disk(self, disk, raid_devices):
        disk_type = disk.get("type")
        logicalname = disk.get("logicalname")
        description = disk.get("description")
        size = disk.get("size")
        product = disk.get("product")
        if (
            logicalname in raid_devices
            or disk_type is None
            or product is None
            or description is None
        ):
            return True
        non_raid_disks = [
            "MR9361-8i",
        ]

        if (
            logicalname in raid_devices
            or product in non_raid_disks
            or "virtual" in product.lower()
            or "logical" in product.lower()
            or "volume" in description.lower()
            or "dvd-ram" in description.lower()
            or description == "SCSI Enclosure"
            or (size is None and logicalname is None)
        ):
            return True
        return False

    def get_hw_disks(self):
        disks = []

        for raid_card in self.get_raid_cards(filter_cards=True):
            disks.extend(raid_card.get_physical_disks())

        raid_devices = [
            d.get("custom_fields", {}).get("vd_device")
            for d in disks
            if d.get("custom_fields", {}).get("vd_device")
        ]

        for disk in self.lshw.get_hw_linux("storage"):
            if self.is_virtual_disk(disk, raid_devices):
                continue
            size = round(int(disk.get("size") or 0) / 1073741824, 1)
            d = {
                "name": "",
                "Size": "{} GB".format(size),
                "logicalname": disk.get("logicalname"),
                "description": disk.get("description"),
                "SN": disk.get("serial"),
                "Model": disk.get("product"),
                "Type": disk.get("type"),
            }
            if disk.get("vendor"):
                d["Vendor"] = disk["vendor"]
            else:
                d["Vendor"] = get_vendor(disk["product"])
            disks.append(d)

        # remove duplicate serials
        seen = set()
        uniq = [x for x in disks if x["SN"] not in seen and not seen.add(x["SN"])]
        return uniq

    def find_or_create_module_type(self, manufacturer, model, part_number=None, description=""):
        # NetBox ModuleType requires a manufacturer; default to "Generic" if missing
        if not manufacturer:
            manufacturer = self.find_or_create_manufacturer("Generic")

        mod_type = nb.dcim.module_types.get(
            manufacturer_id=manufacturer.id,
            model=model,
        )
        if not mod_type:
            parms = {
                "manufacturer": manufacturer.id,
                "model": model,
                "part_number": part_number or "",
                "description": description,
            }
            mod_type = nb.dcim.module_types.create(**parms)
        return mod_type

    def create_netbox_disk(self, disk):
        manufacturer = None
        if disk.get("Vendor"):
            manufacturer = self.find_or_create_manufacturer(disk["Vendor"])

        name = "{} ({})".format(disk["Model"], disk["Size"])
        description = disk["Type"]
        sn = disk.get("SN", "unknown")

        mod_type = self.find_or_create_module_type(
            manufacturer=manufacturer,
            model=name,
            part_number=disk["Model"],
            description=description,
        )

        # Skip creation if a module with this serial already exists on the device
        if sn and sn != "unknown":
            existing_modules = nb.dcim.modules.filter(device_id=self.device_id)
            for mod in existing_modules:
                if mod.serial == sn:
                    if mod.module_type.id != mod_type.id:
                        mod.module_type = mod_type.id
                        mod.save()
                    logging.info(
                        "Disk Module {model} {serial} already exists".format(
                            model=disk["Model"],
                            serial=sn,
                        )
                    )
                    return

        # Find the first unoccupied ModuleBay on the device (natural sorted)
        bays = sorted(
            list(nb.dcim.module_bays.filter(device_id=self.device_id)),
            key=natural_bay_sort_key,
        )
        empty_bay = next((b for b in bays if not b.installed_module), None)
        if not empty_bay:
            logging.warning(
                "No empty ModuleBay available on device {dev} for disk {model} {serial}".format(
                    dev=self.device_id,
                    model=disk["Model"],
                    serial=sn,
                )
            )
            return

        parms = {
            "device": self.device_id,
            "module_bay": empty_bay.id,
            "module_type": mod_type.id,
            "serial": sn if sn != "unknown" else "",
            "status": "active",
            "description": description,
            "tags": [{"name": INVENTORY_TAG["disk"]["name"]}],
        }
        if config.process_virtual_drives:
            parms["custom_fields"] = disk.get("custom_fields", {})

        _ = nb.dcim.modules.create(**parms)

        logging.info(
            "Creating Disk Module {model} {serial} in bay {bay}".format(
                model=disk["Model"],
                serial=sn,
                bay=empty_bay.name,
            )
        )

    def dump_disks_map(self, disks):
        disk_map = [d["custom_fields"] for d in disks if "custom_fields" in d]
        if config.dump_disks_map == "-":
            f = sys.stdout
        else:
            f = open(config.dump_disks_map, "w")
        f.write(json.dumps(disk_map, separators=(",", ":"), indent=4, sort_keys=True))
        if config.dump_disks_map != "-":
            f.close()

    def do_netbox_disks(self):
        try:
            nb_disks = list(
                nb.dcim.modules.filter(
                    device_id=self.device_id,
                    tag=INVENTORY_TAG["disk"]["slug"],
                )
            )
        except pynetbox.core.query.RequestError:
            logging.info(
                "Tag {tag} is missing, returning empty array.".format(
                    tag=INVENTORY_TAG["disk"]["slug"]
                )
            )
            nb_disks = []

        disks = self.get_hw_disks()
        if config.dump_disks_map:
            try:
                self.dump_disks_map(disks)
            except Exception as e:
                logging.error(f"Failed to dump disks map: {e}")
                logging.debug(traceback.format_exc())

        disk_serials = [d["SN"] for d in disks if "SN" in d]

        # Delete disk modules that are in NetBox but no longer present locally
        for nb_disk in nb_disks:
            if nb_disk.serial not in disk_serials or config.force_disk_refresh:
                logging.info(f"Deleting unknown locally Disk Module {nb_disk.serial}")
                nb_disk.delete()

        if config.force_disk_refresh:
            nb_disks = list(
                nb.dcim.modules.filter(
                    device_id=self.device_id,
                    tag=INVENTORY_TAG["disk"]["slug"],
                )
            )

        # Create disk modules that are not in NetBox
        for disk in disks:
            if disk.get("SN") not in [d.serial for d in nb_disks]:
                self.create_netbox_disk(disk)

    def _slots_for_memories(self):
        slots = {}
        for i, mem in enumerate(self.lshw.memories, start=1):
            bay_name = str(mem.get("slot") or f"DIMM {i}")
            size = mem.get("size")
            description = "{size} GB".format(size=size) if size else "Memory"
            slots[bay_name] = {
                "vendor": mem.get("vendor") or "N/A",
                "model": str(mem.get("product") or f"DIMM {i}"),
                "part": mem.get("product") or "",
                "serial": self._normalize_serial(mem.get("serial", "N/A")),
                "description": description,
            }
        return slots

    def do_netbox_memories(self):
        self._sync_modules("memory", self._slots_for_memories())

    def _slots_for_gpus(self):
        gpus = []
        for gpu in self.lshw.get_hw_linux("gpu"):
            # Filters GPU if an expansion bay is detected:
            # The internal (VGA) GPU only goes into the blade inventory,
            # the external (3D) GPU goes into the expansion blade.
            if config.expansion_as_device and self.update_expansion ^ self.is_external_gpu(gpu):
                continue
            gpus.append(gpu)

        slots = {}
        for i, gpu in enumerate(sorted(gpus, key=lambda x: str(x.get("product", ""))), start=1):
            model = str(gpu.get("product") or "Unknown GPU")
            if len(model) > 50:
                model = model[:48] + ".."
            slots[f"GPU {i}"] = {
                "vendor": gpu.get("vendor") or "Unknown",
                "model": model,
                "part": "",
                "serial": "",
                "description": gpu.get("description") or "GPU",
            }
        return slots

    def do_netbox_gpus(self):
        self._sync_modules("gpu", self._slots_for_gpus())

    def create_or_update(self):
        if config.inventory is None or config.update_inventory is None:
            return False
        if self.update_expansion is False:
            self.do_netbox_cpus()
            self.do_netbox_memories()
            self.do_netbox_interfaces()
            self.do_netbox_motherboard()
        self.do_netbox_gpus()
        self.do_netbox_disks()
        self.do_netbox_raid_cards()
        self.purge_old_inventory_items()
        return True

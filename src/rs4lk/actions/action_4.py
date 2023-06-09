from __future__ import annotations

import ipaddress
import logging
import shlex
import time

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab
from Kathara.model.Machine import Machine

from . import action_utils
from .. import utils
from ..foundation.actions.action import Action
from ..foundation.configuration.vendor_configuration import VendorConfiguration
from ..model.topology import Topology


class Action4(Action):
    def verify(self, config: VendorConfiguration, topology: Topology, net_scenario: Lab) -> (bool, str):
        candidate = topology.get(config.get_local_as())

        all_announced_networks = {4: set(), 6: set()}
        # Get all providers
        providers_routers = list(filter(lambda x: x[1].is_provider(), topology.all()))
        if len(providers_routers) == 0:
            logging.warning("No providers found, skipping check...")
            return True

        for _, provider in providers_routers:
            logging.info(f"Reading networks from provider AS{provider.identifier}...")
            device_networks = action_utils.get_bgp_networks(net_scenario.get_machine(provider.name))
            all_announced_networks[4].update(device_networks[4])
            all_announced_networks[6].update(device_networks[6])

        # Remove default
        all_announced_networks[4] = set(filter(lambda x: x.prefixlen != 0, all_announced_networks[4]))
        all_announced_networks[6] = set(filter(lambda x: x.prefixlen != 0, all_announced_networks[6]))

        logging.info("Aggregating networks...")
        utils.aggregate_v4_6_networks(all_announced_networks)
        logging.debug(f"Resulting networks are: {all_announced_networks}")

        customer_routers = list(filter(lambda x: x[1].is_customer(), topology.all()))
        if len(customer_routers) == 0:
            logging.warning("No customers found, skipping check...")
            return True

        passed_checks = []
        for v, networks in all_announced_networks.items():
            logging.info(f"Performing check on IPv{v}...")

            if not networks:
                logging.warning(f"No networks announced in IPv{v}, skipping...")
                continue

            spoofing_net = action_utils.get_non_overlapping_network(v, networks)
            logging.info(f"Chosen network to announce is {spoofing_net}.")

            for _, customer in customer_routers:
                candidate_neighbour, _ = customer.get_neighbour_by_name(candidate.name)
                if not candidate_neighbour:
                    logging.info(f"Skipping AS{customer.identifier} since it is not directly connected.")
                    continue

                candidate_ips = candidate_neighbour.get_ips(is_public=True)
                if len(candidate_ips[v]) == 0:
                    logging.warning(f"No networks announced in IPv{v} from "
                                    f"customer AS{customer.identifier} towards candidate AS, skipping...")
                    continue

                customer_device = net_scenario.get_machine(customer.name)

                # Announce the spoofed network from the customer
                self._vtysh_network(customer_device, customer.identifier, spoofing_net)

                logging.info("Waiting 20s before performing check...")
                time.sleep(20)

                for _, provider in providers_routers:
                    provider_device = net_scenario.get_machine(provider.name)

                    _, candidate_iface_idx = provider.get_node_by_name(candidate.name)
                    # We can surely pop since there is only one public IP towards the candidate router
                    (cand_peering_ip, _) = provider.neighbours[candidate_iface_idx].get_ips(is_public=True)[v].pop()
                    candidate_nets = action_utils.get_neighbour_bgp_networks(provider_device, cand_peering_ip.ip)

                    result = spoofing_net not in candidate_nets
                    passed_checks.append(result)
                    if result:
                        logging.success(f"Check passed on IPv{v} with provider AS{provider.identifier}!")
                    else:
                        logging.warning(f"Check not passed on IPv{v} with provider AS{provider.identifier}!")

                self._no_vtysh_network(customer_device, customer.identifier, spoofing_net)

        return all(passed_checks)

    @staticmethod
    def _vtysh_network(device: Machine, as_num: int, net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> None:
        logging.info(f"Announcing Network={net} in device `{device.name}`.")

        v = net.version
        exec_output = Kathara.get_instance().exec(
            machine_name=device.name,
            command=shlex.split(f"vtysh "
                                f"-c 'configure' "
                                f"-c 'router bgp {as_num}' "
                                f"-c 'address-family ipv{v} unicast' "
                                f"-c 'network {net}' "
                                f"-c 'exit' -c 'exit' -c 'exit'"),
            lab_name=device.lab.name
        )

        # Triggers the command.
        try:
            next(exec_output)
        except StopIteration:
            pass

    @staticmethod
    def _no_vtysh_network(device: Machine, as_num: int, net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> None:
        logging.info(f"Removing Network={net} announcement in device `{device.name}`.")

        v = net.version
        exec_output = Kathara.get_instance().exec(
            machine_name=device.name,
            command=shlex.split(f"vtysh "
                                f"-c 'configure' "
                                f"-c 'router bgp {as_num}' "
                                f"-c 'address-family ipv{v} unicast' "
                                f"-c 'no network {net}' "
                                f"-c 'exit' -c 'exit' -c 'exit'"),
            lab_name=device.lab.name
        )

        # Triggers the command.
        try:
            next(exec_output)
        except StopIteration:
            pass

    def name(self) -> str:
        return "leak"

    def display_name(self) -> str:
        return "Route Leak Check"

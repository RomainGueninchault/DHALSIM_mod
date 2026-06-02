import argparse
import os.path
import random
import signal
import sqlite3
import sys
import time
from pathlib import Path

import yaml
from basePLC import BasePLC
from dhalsim import py3_logger


class Error(Exception):
    """Base class for exceptions in this module."""


class InvalidCommand(Error):
    """Raised when an HMI command is malformed or unsupported."""


class DatabaseError(Error):
    """Raised when the SQLite database cannot be reached."""


class GenericHMI(BasePLC):
    """
    Represents an HMI node in a WTS simulation (Purdue level 2).

    Architecture :
        - La HMI ne parle JAMAIS directement aux PLCs
        - Elle poll le SCADA en lecture pour afficher l'état du système
        - Elle envoie ses commandes au SCADA en écriture
        - Le SCADA se charge de relayer les commandes aux PLCs à sa prochaine
          itération de synchronisation

    Communication :
        Lecture  : HMI  --ENIP read-->  SCADA
        Écriture : HMI  --ENIP write--> SCADA  --ENIP write--> PLC (via SCADA)

    Commandes configurables dans le YAML sous la clé 'hmi' :

        hmi:
          commands:
            - actuator: MV101
              action: open
              trigger:
                type: time    # 'time', 'above', 'below'
                value: 50
            - actuator: P101
              action: closed
              trigger:
                type: above
                sensor: T101
                value: 0.9

    Note : les commandes sont envoyées au SCADA, pas aux PLCs.
    Le SCADA les détecte comme des écritures sur ses propres tags
    et les relaie aux PLCs lors de sa prochaine itération sync.
    """

    DB_TRIES = 10
    HMI_LOOP_SLEEP = 0.5

    def __init__(self, intermediate_yaml_path):
        with intermediate_yaml_path.open() as yaml_file:
            self.intermediate_yaml = yaml.load(yaml_file, Loader=yaml.FullLoader)

        self.logger = py3_logger.get_logger(self.intermediate_yaml['log_level'])
        self.hmi_run = True
        self.db_sleep_time = random.uniform(0.01, 0.1)
        self._fired_time_commands = set()

        # IP du SCADA — seul interlocuteur de la HMI
        self.scada_ip = self.intermediate_yaml['scada']['public_ip']

        # Tags exposés par le SCADA (tous les tags de tous les PLCs)
        self.scada_tags = self._get_scada_tags()
        # Tags _CMD — un par actuateur, pour envoyer des commandes au SCADA
        self.cmd_tags = [tag + '_CMD' for tag in self._get_actuator_tags()]

        # Commandes HMI depuis le YAML
        self.commands = self._parse_commands()

        # Serveur ENIP de la HMI — expose tags de monitoring + tags _CMD
        all_tags = self.scada_tags + self.cmd_tags
        real_tags = tuple((tag, 1, 'REAL') for tag in all_tags)

        state = {
            'name': 'plant',
            'path': self.intermediate_yaml['db_path']
        }

        hmi_server = {
            'address': self.intermediate_yaml['hmi']['local_ip'],
            'tags': real_tags
        }

        hmi_protocol = {
            'name': 'enip',
            'mode': 1,
            'server': hmi_server
        }

        super(GenericHMI, self).__init__(
            name='hmi', state=state, protocol=hmi_protocol
        )

    # ------------------------------------------------------------------
    # Helpers d'initialisation
    # ------------------------------------------------------------------

    def _get_scada_tags(self):
        """
        Retourne la liste de tous les tags (capteurs + actionneurs)
        exposés par le SCADA — c'est la même liste que generate_real_tags
        dans generic_scada.py.
        """
        tags = []
        for plc in self.intermediate_yaml['plcs']:
            for sensor in plc.get('sensors', []):
                if sensor:
                    tags.append(sensor)
            for actuator in plc.get('actuators', []):
                if actuator:
                    tags.append(actuator)
        return tags

    def _get_actuator_tags(self):
        """Retourne uniquement les tags d'actionneurs — utilisés pour les tags _CMD."""
        tags = []
        for plc in self.intermediate_yaml['plcs']:
            for actuator in plc.get('actuators', []):
                if actuator:
                    tags.append(actuator)
        return tags

    def _parse_commands(self):
        hmi_section = self.intermediate_yaml.get('hmi', {})
        return hmi_section.get('commands', [])

    # ------------------------------------------------------------------
    # SQLite
    # ------------------------------------------------------------------

    def db_query(self, query, write=False, parameters=None):
        for i in range(self.DB_TRIES):
            try:
                with sqlite3.connect(self.intermediate_yaml['db_path']) as conn:
                    cur = conn.cursor()
                    if parameters:
                        cur.execute(query, parameters)
                    else:
                        cur.execute(query)
                    conn.commit()
                    if not write:
                        return cur.fetchone()[0]
                    else:
                        return
            except sqlite3.OperationalError as exc:
                self.logger.info("DB error: {e}. Retrying {n} more times.".format(
                    e=exc, n=self.DB_TRIES - i - 1))
                time.sleep(self.db_sleep_time)
        raise DatabaseError("Could not reach database after {n} tries.".format(n=self.DB_TRIES))

    def get_master_clock(self):
        return self.db_query("SELECT time FROM master_time WHERE id IS 1", False, None)

    # ------------------------------------------------------------------
    # Communication avec le SCADA uniquement
    # ------------------------------------------------------------------

    def read_from_scada(self, tag):
        """
        Lit la valeur d'un tag depuis le serveur ENIP du SCADA.
        C'est la seule source de données pour la HMI.
        """
        try:
            return float(self.receive((tag, 1), self.scada_ip))
        except Exception as exc:
            self.logger.warning("HMI could not read {tag} from SCADA ({ip}): {exc}".format(
                tag=tag, ip=self.scada_ip, exc=exc))
            return None

    def write_to_scada(self, tag, value):
        """
        Écrit une commande sur le tag _CMD correspondant du serveur ENIP du SCADA.
        Ex: pour commander MV101, on écrit sur MV101_CMD.
        Le SCADA détecte MV101_CMD != -1, relaie au PLC, remet à -1.
        """
        cmd_tag = tag + '_CMD'
        try:
            self.send((cmd_tag, 1), value, self.scada_ip)
            self.logger.info("HMI wrote {tag}={val} to SCADA (via {cmd})".format(
                tag=tag, val=value, cmd=cmd_tag))
        except Exception as exc:
            self.logger.error("HMI failed to write {cmd}={val} to SCADA: {exc}".format(
                cmd=cmd_tag, val=value, exc=exc))

    # ------------------------------------------------------------------
    # Évaluation des triggers
    # ------------------------------------------------------------------

    def _should_trigger(self, trigger, current_clock):
        t_type = trigger['type'].lower()

        if t_type == 'time':
            target = trigger['value']
            cmd_id = id(trigger)
            if current_clock >= target and cmd_id not in self._fired_time_commands:
                self._fired_time_commands.add(cmd_id)
                return True
            return False

        elif t_type in ('above', 'below'):
            sensor = trigger.get('sensor')
            threshold = trigger.get('value')
            if sensor is None or threshold is None:
                self.logger.warning("Trigger above/below missing sensor or value.")
                return False
            # La HMI lit le capteur depuis le SCADA, pas depuis le PLC
            current_value = self.read_from_scada(sensor)
            if current_value is None:
                return False
            if t_type == 'above':
                return current_value > threshold
            else:
                return current_value < threshold

        self.logger.warning("Unknown trigger type: {t}".format(t=t_type))
        return False

    def apply_commands(self, current_clock):
        """
        Évalue les commandes configurées et envoie celles dont le trigger
        est satisfait — vers le SCADA, pas vers les PLCs directement.
        """
        for cmd in self.commands:
            trigger = cmd.get('trigger', {})
            if not self._should_trigger(trigger, current_clock):
                continue

            actuator = cmd['actuator']
            action = cmd['action']

            if isinstance(action, str):
                if action.lower() == 'open':
                    value = 1
                elif action.lower() == 'closed':
                    value = 0
                else:
                    self.logger.error("Unknown action: {a}".format(a=action))
                    continue
            else:
                value = action

            self.write_to_scada(actuator, value)

    # ------------------------------------------------------------------
    # Lifecycle MiniCPS
    # ------------------------------------------------------------------

    def pre_loop(self, sleep=0.5):
        self.logger.debug('HMI enters pre_loop')
        signal.signal(signal.SIGINT, self.sigint_handler)
        signal.signal(signal.SIGTERM, self.sigint_handler)
        time.sleep(sleep)

    def sigint_handler(self, sig, frame):
        self.logger.debug('HMI shutdown.')
        self.hmi_run = False
        sys.exit(0)

    def main_loop(self, sleep=0.5, test_break=False):
        """
        Boucle principale de la HMI.
        Asynchrone — ne participe pas à la boucle de sync SQLite.

        À chaque itération :
          1. Poll tous les tags depuis le SCADA (monitoring)
          2. Évalue et envoie les commandes configurées vers le SCADA
          3. Attend HMI_LOOP_SLEEP secondes
        """
        self.logger.debug('HMI enters main_loop')

        while self.hmi_run:
            current_clock = self.get_master_clock()

            # Phase lecture — poll le SCADA uniquement
            for tag in self.scada_tags:
                value = self.read_from_scada(tag)
                if value is not None:
                    self.logger.debug('HMI read {tag}={val} from SCADA'.format(
                        tag=tag, val=value))

            # Phase écriture — envoie les commandes au SCADA
            self.apply_commands(current_clock)

            time.sleep(self.HMI_LOOP_SLEEP)

            if test_break:
                break


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def is_valid_file(parser_instance, arg):
    if not os.path.exists(arg):
        parser_instance.error(arg + " does not exist.")
    else:
        return arg


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Start a generic HMI node')
    parser.add_argument(
        dest="intermediate_yaml",
        help="Path to the intermediate YAML file",
        metavar="FILE",
        type=lambda x: is_valid_file(parser, x)
    )
    args = parser.parse_args()
    hmi = GenericHMI(intermediate_yaml_path=Path(args.intermediate_yaml))
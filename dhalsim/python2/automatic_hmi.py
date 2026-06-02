import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

from automatic_node import NodeControl
from dhalsim.py3_logger import get_logger

empty_loc = '/dev/null'


class HMIControl(NodeControl):
    """
    This class is started for an HMI node.
    It starts a tcpdump capture and the HMI process (generic_hmi.py).

    Follows the exact same pattern as ScadaControl in automatic_scada.py.
    """

    PROCESS_TIMEOUT = 1.0
    """Timeout between sending SIGINT, SIGTERM, and SIGKILL."""

    def __init__(self, intermediate_yaml):
        super(HMIControl, self).__init__(intermediate_yaml)

        self.logger = get_logger(self.data['log_level'])

        self.output_path = Path(self.data['output_path'])
        self.process_tcp_dump = None
        self.hmi_process = None

    def terminate_process(self, process):
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            process.wait(self.PROCESS_TIMEOUT)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.terminate()
            if process.poll() is None:
                process.kill()

    def terminate(self):
        """Stop the tcpdump and HMI process."""
        self.logger.debug("Stopping tcpdump process on HMI.")
        self.terminate_process(self.process_tcp_dump)
        self.logger.debug("tcpdump process stopped on HMI.")

        self.logger.debug("Stopping HMI.")
        self.terminate_process(self.hmi_process)
        self.logger.debug("HMI stopped.")

    def main(self):
        """Start tcpdump and HMI process, then wait for HMI to finish."""
        self.process_tcp_dump = self.start_tcpdump_capture()
        self.hmi_process = self.start_hmi()

        while self.hmi_process.poll() is None:
            pass

        self.terminate()

    def start_tcpdump_capture(self):
        """Start a tcpdump capture on the HMI interface."""
        pcap = self.output_path / "hmi-eth0.pcap"
        no_output = open(empty_loc, 'w')
        tcp_dump = subprocess.Popen(
            ['tcpdump', '-i', self.data['hmi']['interface'], '-w', str(pcap)],
            shell=False, stdout=no_output, stderr=no_output
        )
        return tcp_dump

    def start_hmi(self):
        """Start the generic_hmi.py process."""
        generic_hmi_path = Path(__file__).parent.absolute() / "generic_hmi.py"

        if self.data['log_level'] == 'debug':
            err_put = sys.stderr
            out_put = sys.stdout
        else:
            err_put = open(empty_loc, 'w')
            out_put = open(empty_loc, 'w')

        cmd = ["python3", str(generic_hmi_path), str(self.intermediate_yaml)]
        hmi_process = subprocess.Popen(cmd, shell=False, stderr=err_put, stdout=out_put)
        return hmi_process


def is_valid_file(parser_instance, arg):
    if not os.path.exists(arg):
        parser_instance.error(arg + " does not exist.")
    else:
        return arg


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Start everything for an HMI node')
    parser.add_argument(
        dest="intermediate_yaml",
        help="intermediate yaml file",
        metavar="FILE",
        type=lambda x: is_valid_file(parser, x)
    )

    args = parser.parse_args()
    hmi_control = HMIControl(Path(args.intermediate_yaml))
    hmi_control.main()
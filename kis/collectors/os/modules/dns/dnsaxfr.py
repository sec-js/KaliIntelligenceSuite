# -*- coding: utf-8 -*-
"""
run tool host on each in-scope second-level domain (e.g., megacorpone.com) using the operating system's DNS server as
well as on each in-scope DNS service to test for DNS zone transfers.
"""

__author__ = "Lukas Reiter"
__license__ = "GPL v3.0"
__copyright__ = """Copyright 2018 Lukas Reiter

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
__version__ = 0.1

import re
import logging
from typing import List
from collectors.os.modules.core import DomainCollector
from collectors.os.modules.core import ServiceCollector
from collectors.os.modules.dns.core import BaseDnsCollector
from collectors.os.modules.core import BaseCollector
from collectors.os.core import PopenCommand
from database.model import HostName
from database.model import DomainName
from database.model import ScopeType
from database.model import Service
from database.model import Command
from database.model import CollectorName
from database.model import Source
from database.model import DnsResourceRecordType
from view.core import ReportItem
from sqlalchemy import or_
from sqlalchemy.orm.session import Session

logger = logging.getLogger('dnsaxfr')


class CollectorClass(BaseDnsCollector, DomainCollector, ServiceCollector):
    """This class implements a collector module that is automatically incorporated into the application."""

    def __init__(self, **kwargs):
        super().__init__(priority=1305,
                         timeout=0,
                         **kwargs)
        self._re_entry = re.compile("^(?P<hostname>.+?)\.\s+\d+\s+IN\s+(?P<type>[A-Z]+)\s+(\d+\s)?(?P<content>.+)$",
                                    re.IGNORECASE)

    @staticmethod
    def get_argparse_arguments():
        return {"help": __doc__, "action": "store_true"}

    def create_domain_commands(self,
                               session: Session,
                               host_name: HostName,
                               collector_name: CollectorName) -> List[BaseCollector]:
        """This method creates and returns a list of commands based on the given service.

        This method determines whether the command exists already in the database. If it does, then it does nothing,
        else, it creates a new Collector entry in the database for each new command as well as it creates a corresponding
        operating system command and attaches it to the respective newly created Collector class.

        :param session: Sqlalchemy session that manages persistence operations for ORM-mapped objects
        :param host_name: The host name based on which commands shall be created.
        :param collector_name: The name of the collector as specified in table collector_name
        :return: List of Collector instances that shall be processed.
        """
        collectors = []
        if host_name and host_name.name is None:
            os_command = [self._path_host, '-t', "axfr", host_name.full_name]
            if self._dns_server:
                os_command.append(self._dns_server)
            collector = self._get_or_create_command(session, os_command, collector_name, host_name=host_name)
            collectors.append(collector)
        return collectors

    def create_service_commands(self,
                                session: Session,
                                service: Service,
                                collector_name: CollectorName) -> List[BaseCollector]:
        """This method creates and returns a list of commands based on the given service.

        This method determines whether the command exists already in the database. If it does, then it does nothing,
        else, it creates a new Collector entry in the database for each new command as well as it creates a corresponding
        operating system command and attaches it to the respective newly created Collector class.

        :param session: Sqlalchemy session that manages persistence operations for ORM-mapped objects
        :param service: The service based on which commands shall be created.
        :param collector_name: The name of the collector as specified in table collector_name
        :return: List of Collector instances that shall be processed.
        """
        collectors = []
        if self.match_nmap_service_name(service) and not self._dns_server:
            domain_name = session.query(DomainName).filter(or_(DomainName.scope == ScopeType.all,
                                                               DomainName.scope == ScopeType.strict)).all()
            for item in domain_name:
                os_command = [self._path_host,
                              "-{}".format(service.host.version),
                              "-t", "axfr",
                              "-p", service.port,
                              item.name,
                              service.address]
                collector = self._get_or_create_command(session, os_command, collector_name, service=service)
                collectors.append(collector)
        return collectors

    def verify_results(self, session: Session,
                       command: Command,
                       source: Source,
                       report_item: ReportItem,
                       process: PopenCommand = None, **kwargs) -> None:
        """This method analyses the results of the command execution.

        After the execution, this method checks the OS command's results to determine the command's execution status as
        well as existing vulnerabilities (e.g. weak login credentials, NULL sessions, hidden Web folders). The
        stores the output in table command. In addition, the collector might add derived information to other tables as
        well.

        :param session: Sqlalchemy session that manages persistence operations for ORM-mapped objects
        :param command: The command instance that contains the results of the command execution
        :param source: The source object of the current collector
        :param report_item: Item that can be used for reporting potential findings in the UI
        :param process: The PopenCommand object that executed the given result. This object holds stderr, stdout, return
        code etc.
        """
        command.hide = True
        for line in command.stdout_output:
            match = self._re_entry.match(line)
            if match:
                host_name_str = match.group("hostname").strip(". ")
                record_type_str = match.group("type").strip().lower()
                content = match.group("content").strip().strip(". ")
                try:
                    record_type = DnsResourceRecordType[record_type_str]
                    host_name = self.add_host_name(session=session,
                                                   command=command,
                                                   host_name=host_name_str,
                                                   source=source,
                                                   report_item=report_item)
                    if host_name:
                        if record_type in [DnsResourceRecordType.a, DnsResourceRecordType.aaaa] and content:
                            # Add IPv4 address to database
                            host = self.add_host(session=session,
                                                 command=command,
                                                 source=source,
                                                 address=content,
                                                 report_item=report_item)
                            if not host:
                                logger.debug("ignoring host due to invalid IP address in line: {}".format(line))
                            else:
                                self.add_host_host_name_mapping(session=session,
                                                                command=command,
                                                                host=host,
                                                                host_name=host_name,
                                                                source=source,
                                                                mapping_type=record_type,
                                                                report_item=report_item)
                        if record_type == DnsResourceRecordType.cname and content:
                            cname_host_name = self.add_host_name(session=session,
                                                                 command=command,
                                                                 host_name=content,
                                                                 source=source,
                                                                 report_item=report_item)
                            if cname_host_name:
                                self.add_host_name_host_name_mapping(session=session,
                                                                     command=command,
                                                                     source_host_name=host_name,
                                                                     resolved_host_name=cname_host_name,
                                                                     source=source,
                                                                     mapping_type=DnsResourceRecordType.cname,
                                                                     report_item=report_item)
                            else:
                                logger.debug("ignoring host name due to invalid domain in line: {}".format(line))

                        else:
                            for item in self._domain_utils.extract_domains(content):
                                host_name = self.add_host_name(session=session,
                                                               command=command,
                                                               host_name=item,
                                                               source=source,
                                                               report_item=report_item)
                                if not host_name:
                                    logger.debug("ignoring host name due to invalid domain in line: {}".format(line))
                    else:
                        logger.debug("ignoring host name due to invalid domain in line: {}".format(line))
                except KeyError as ex:
                    logger.exception(ex)
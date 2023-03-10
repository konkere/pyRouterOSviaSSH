#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
from time import sleep
from threading import Thread
from datetime import datetime
from argparse import ArgumentParser
from os import path, mkdir, environ, stat
from netmiko import ConnectHandler, file_transfer
from related_utils import generate_device, allowed_filename, print_output, size_converter
from related_utils import remove_old_files, generate_telegram_bot, markdownv2_converter


def args_parser():
    parser = ArgumentParser(description='RouterOS backuper.')
    parser.add_argument('-s', '--sshconf', type=str, help='Path to ssh_config.', required=False)
    parser.add_argument('-n', '--host', type=str, help='Single Host (in ssh_config).', required=False)
    parser.add_argument('-l', '--hostlist', type=str, help='Path to file with list of Hosts.', required=False)
    parser.add_argument('-p', '--path', type=str, help='Path to backups.', required=True)
    parser.add_argument('-t', '--lifetime', type=int, help='Files (backup) lifetime (in days).', required=False)
    parser.add_argument('-b', '--bottoken', type=str, help='Telegram Bot token.', required=False)
    parser.add_argument('-c', '--chatid', type=str, help='Telegram chat id.', required=False)
    arguments = parser.parse_args().__dict__
    return arguments


def hosts_to_devices(hosts):
    devices = []
    ssh_config_file = args_in['sshconf'] if args_in['sshconf'] else path.join(environ.get('HOME'), '.ssh/config')
    for hostname in hosts:
        hostname = hostname.strip()
        if hostname:
            host_device = Backuper(
                ssh_config_file=ssh_config_file,
                host=hostname,
                path_to_backups=args_in['path'],
                lifetime=args_in['lifetime']
            )
            devices.append(host_device)
    return devices


def summary_report(reports, lifetime):
    many_hosts = len(reports) > 1
    ending = {
        True: 'ะฐั',
        False: 'ะต',
    }
    emoji_dead = '\U0001F480'       # ๐
    message_header = f'ะัััั ะพ ะฟัะพะฒะตะดะตะฝะธะธ ะฑัะบะฐะฟะฐ ะฝะฐัััะพะตะบ ะฝะฐ ะะธะบัะพัะธะบ{ending[many_hosts]}.\n\n'
    message_body = ''
    message_footer = ''
    if lifetime:
        message_footer += f'{emoji_dead}ะขะฐะบะถะต ะฑัะปะธ ัะดะฐะปะตะฝั ัะฐะฝะตะต ัะพััะฐะฝัะฝะฝัะต ะฑัะบะฐะฟั ััะฐััะต {lifetime} ะดะฝ.'
    for report in reports:
        message_body += f'{report}\n'
    message = markdownv2_converter(message_header) + message_body + markdownv2_converter(message_footer)
    return message


class Backuper(Thread):

    def __init__(self, host, path_to_backups, ssh_config_file, lifetime, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.path_to_backups = path_to_backups
        self.mikrotik_router = generate_device(ssh_config_file, host)
        self.connect = ConnectHandler(**self.mikrotik_router)
        self.lifetime = lifetime
        self.subdir = 'backup'
        self.delay = 1
        self.report = ''
        self.emoji = {
            'device':   '\U0001F4F6',       # ๐ถ
            'dir':      '\U0001F4C2',       # ๐
            'ok':       '\U00002705',       # โ
            'not ok':   '\U0000274E',       # โ
        }

    def run(self):
        self.connect.enable()
        identity = self.generate_identity()
        path_to_backup = path.join(self.path_to_backups, identity)
        backup_name = f'{identity}_{datetime.now().strftime("%Y.%m.%d_%H.%M.%S.%f")}'
        self.make_dirs(path_to_backup)
        self.create_backup(backup_name)
        self.add_to_report(f'ะ ะบะฐัะฐะปะพะณะต {self.emoji["dir"]}`{markdownv2_converter(path_to_backup)}/` ัะพััะฐะฝะตะฝั ัะฐะนะปั:')
        for backup_type in ['rsc', 'backup']:
            self.download_backup(backup_type, backup_name, path_to_backup)
            self.remove_backup_from_device(backup_type, backup_name)
        self.connect.disconnect()
        if self.lifetime:
            remove_old_files(path_to_backup, self.lifetime)

    def add_to_report(self, text, paragraph=False):
        self.report += '\n' * paragraph + f'{text}\n'

    def generate_identity(self):
        command = '/system identity print'
        identity = print_output(self.connect, command)
        identity_name = re.match(r'^name: (.*)$', identity).group(1)
        self.add_to_report(f'{self.emoji["device"]}*{markdownv2_converter(identity_name)}*')
        allowed_identity_name = allowed_filename(identity_name)
        return allowed_identity_name

    def make_dirs(self, path_to_backup):
        try:
            mkdir(path_to_backup)
        except FileExistsError:
            pass
        command = f'/file print detail where name={self.subdir}'
        backup_dir = print_output(self.connect, command)
        if not backup_dir:
            # Crutch for create directory
            self.connect.send_command(f'/ip smb shares add directory={self.subdir} name=crutch_for_dir')
            self.connect.send_command('/ip smb shares remove [/ip smb shares find where name=crutch_for_dir]')

    def create_backup(self, backup_name):
        file_path_name = f'{self.subdir}/{backup_name}'
        self.connect.send_command(f'/export file={file_path_name}')
        self.connect.send_command(f'/system backup save dont-encrypt=yes name={file_path_name}')
        # Wait for files creation
        sleep(self.delay)

    def download_backup(self, backup_type, backup_name, path_to_backup):
        src_file = f'{backup_name}.{backup_type}'
        dst_file = f'{path_to_backup}/{backup_name}.{backup_type}'
        direction = 'get'
        try:
            transfer_dict = file_transfer(
                self.connect,
                source_file=src_file,
                dest_file=dst_file,
                file_system=self.subdir,
                direction=direction,
                overwrite_file=True,
            )
        # Bug in scp_handler.py โ https://github.com/ktbyers/netmiko/issues/2818 (fixed only in develop branch)
        except ValueError:
            pass
        # Wait for file download
        sleep(self.delay)
        file_name = markdownv2_converter(src_file)
        try:
            file_stats = stat(dst_file)
        except FileNotFoundError:
            file_info = f'{self.emoji["not ok"]}{file_name}'
        else:
            file_size = markdownv2_converter(size_converter(file_stats.st_size))
            file_name = markdownv2_converter(src_file)
            file_info = f'{self.emoji["ok"]}{file_name} โ {file_size}'
        self.add_to_report(file_info)

    def remove_backup_from_device(self, backup_type, backup_name):
        self.connect.send_command(f'/file remove {self.subdir}/{backup_name}.{backup_type}')


def main():
    if args_in['hostlist']:
        with open(args_in['hostlist']) as file:
            hosts_list = file.readlines()
    elif args_in['host']:
        hosts_list = [args_in['host']]
    else:
        exit(0)
    telegram_bot = generate_telegram_bot(args_in['bottoken'], args_in['chatid'])
    devices_backup = hosts_to_devices(hosts_list)
    for device in devices_backup:
        device.start()
    for device in devices_backup:
        device.join()
    if telegram_bot and telegram_bot.alive():
        devices_reports = []
        for device in devices_backup:
            devices_reports.append(device.report)
        report = summary_report(devices_reports, args_in['lifetime'])
        telegram_bot.send_text_message(report)


if __name__ == '__main__':
    args_in = args_parser()
    main()

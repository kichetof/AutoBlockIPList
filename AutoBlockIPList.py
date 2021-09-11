#!/usr/bin/env python3

import os
import shutil
from datetime import datetime
import validators
import argparse
import requests
import sqlite3
import ipaddress
import time
from functools import reduce


VERSION = "1.0.0"


def create_connection(db_file):
    try:
        return sqlite3.connect(db_file)
    except sqlite3.Error as e:
        raise e


def get_ip_remote(link):
    data = ""
    try:
        r = requests.get(link)
        r.raise_for_status()
        data = r.text.replace("\r", "")
    except requests.exceptions.RequestException as e:
        verbose(f"Unable to connect to {link}")
    return data


def get_ip_local(file):
    return file.read().replace("\r", "")


def get_ip_list(local, external):
    data = [get_ip_local(f).split("\n") for f in local] + [get_ip_remote(s).split("\n") for s in external]
    ip = reduce(lambda a, b: a + b, data)
    return ip


def ipv4_to_ipstd(ipv4):
    numbers = [int(bits) for bits in ipv4.split('.')]
    return '0000:0000:0000:0000:0000:ffff:{:02x}{:02x}:{:02x}{:02x}'.format(*numbers).upper()


def ipv6_to_ipstd(ipv6):
    return ipaddress.ip_address(ipv6).exploded.upper()


def process_ip(ip_list, expire):
    processed = []
    invalid = []
    for i in ip_list:
        try:
            ip = ipaddress.ip_address(i)
            if ip.version == 4:
                ipstd = ipv4_to_ipstd(i)
            elif ip.version == 6:
                ipstd = ipv6_to_ipstd(i)
            else:
                ipstd = ""
            processed.append([i, ipstd, expire])
        except ValueError:
            if i != "":
                invalid.append(i)
    return processed, invalid


def url(link):
    validator = validators.url(link)
    if isinstance(validator, validators.ValidationFailure):
        raise argparse.ArgumentError
    return link


def folder(attr='r'):
    def check_folder(path):
        if os.path.isdir(path):
            if attr == 'r' and not os.access(path, os.R_OK):
                raise argparse.ArgumentTypeError(f'"{path}" is not readable.')
            if attr == 'w' and not os.access(path, os.W_OK):
                raise argparse.ArgumentTypeError(f'"{path}" is not writable.')
            return os.path.abspath(path)
        else:
            raise argparse.ArgumentTypeError(f'"{path}" is not a valid path.')
    return check_folder


def verbose(message):
    global args
    if args.verbose:
        print(message)


def parse_args():
    parser = argparse.ArgumentParser(prog='AutoBlockIPList')
    parser.add_argument("-f","--in-file", nargs='*', type=argparse.FileType('r'), default=[],
                        help="Local list file separated by a space "
                             "(eg. /home/user/list.txt custom.txt)")
    parser.add_argument("-u", "--in-url", nargs="*", type=url, default=[],
                        help="External list url separated by a space "
                             "(eg https://example.com/list.txt https://example.com/all.txt)")
    parser.add_argument("-e", "--expire-in-day", type=int, default=0,
                        help="Expire time in day. Default 0: no expiration")
    parser.add_argument("--remove-expired", action='store_true',
                        help="Remove expired entry")
    parser.add_argument("-b", "--backup-to", type=folder('w'),
                        help="Folder to store a backup of the database")
    parser.add_argument("--clear-db", action='store_true',
                        help="Clear ALL deny entry in database before filling")
    parser.add_argument("--dry-run", action='store_true',
                        help="Perform a run without any modifications")
    parser.add_argument("-v", "--verbose", action='store_true',
                        help="Increase output verbosity")
    parser.add_argument('--version', action='version', version=f'%(prog)s version {VERSION}')

    a = parser.parse_args()

    if len(a.in_file) == 0 and len(a.in_url) == 0:
        raise parser.error("At least one source list is mandatory (file or url)")
    if a.clear_db and a.backup_to is None:
        raise parser.error("backup folder should be set for clear db")
    if a.dry_run:
        a.verbose = True

    return a


if __name__ == '__main__':
    start_time = time.time()
    args = parse_args()

    # define the path of the database
    # DSM 6: "/etc/synoautoblock.db"
    # DSM 7: should be the same (TODO confirm path)
    db = "/etc/synoautoblock.db"

    if not os.path.isfile(db):
        raise FileNotFoundError(f"No such file or directory: '{db}'")
    if not os.access(db, os.R_OK):
        raise FileExistsError("Unable to read database. Run this script with sudo or root user.")

    if args.backup_to is not None:
        filename = datetime.now().strftime("%Y%d%m_%H%M%S") + "_backup_synoautoblock.db"
        shutil.copy(db, os.path.join(args.backup_to, filename))
        verbose("Database successfully backup")

    if args.expire_in_day > 0:
        args.expire_in_day = int(start_time) + args.expire_in_day * 60 * 60 * 24

    ips = get_ip_list(args.in_file, args.in_url)
    ips_formatted, ips_invalid = process_ip(ips, args.expire_in_day)

    verbose(f"Total IP fetch in lists: {len(ips_formatted)}")

    if len(ips_formatted) > 0:
        conn = create_connection(db)
        c = conn.cursor()

        if args.remove_expired and not args.dry_run:
            c.execute("DELETE FROM AutoBlockIP WHERE Deny = 1 AND ExpireTime > 0 AND ExpireTime < strftime('%s','now')")
            verbose("All expired entry was successfully removed")

        if args.clear_db and not args.dry_run:
            c.execute("DELETE FROM AutoBlockIP WHERE Deny = 1")
            verbose("All deny entry was successfully removed")

        nb_ip = c.execute("SELECT COUNT(IP) FROM AutoBlockIP WHERE Deny = 1")
        nb_ip_before = nb_ip.fetchone()[0]

        verbose(f"Total deny IP currently in your Synology DB: {nb_ip_before}")
        if not args.dry_run:
            c.executemany("REPLACE INTO AutoBlockIP (IP, IPStd, ExpireTime, Deny, RecordTime, Type, Meta) "
                          "VALUES(?, ?, ?, 1, strftime('%s','now'), 0, NULL);", ips_formatted)
        else:
            verbose("Dry run -> nothing to do")
        nb_ip = c.execute("SELECT COUNT(IP) FROM AutoBlockIP WHERE Deny = 1")
        nb_ip_after = nb_ip.fetchone()[0]
        conn.commit()
        conn.close()
        verbose(f"Total deny IP now in your Synology DB: {nb_ip_after} ({nb_ip_after - nb_ip_before} added)")
    else:
        verbose("No IP found in list")

    elapsed = round(time.time() - start_time, 2)
    verbose(f"Elapsed time: {elapsed} seconds")

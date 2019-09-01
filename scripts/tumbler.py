#! /usr/bin/env python
from __future__ import (absolute_import, division,
                        print_function, unicode_literals)
from builtins import * # noqa: F401

import sys
from twisted.internet import reactor
import os
import pprint
from twisted.python.log import startLogging
from jmclient import Taker, load_program_config, get_schedule,\
    JMClientProtocolFactory, start_reactor, jm_single, get_wallet_path,\
    open_test_wallet_maybe, sync_wallet, get_tumble_schedule,\
    schedule_to_text, estimate_tx_fee, restart_waiter,\
    get_tumble_log, tumbler_taker_finished_update,\
    tumbler_filter_orders_callback
from jmbase.support import get_log, jmprint
from cli_options import get_tumbler_parser, get_max_cj_fee_values, \
     check_regtest

log = get_log()
logsdir = os.path.join(os.path.dirname(
    jm_single().config_location), "logs")


def main():
    tumble_log = get_tumble_log(logsdir)
    (options, args) = get_tumbler_parser().parse_args()
    options_org = options
    options = vars(options)
    if len(args) < 1:
        jmprint('Error: Needs a wallet file', "error")
        sys.exit(0)
    load_program_config()

    check_regtest()

    #Load the wallet
    wallet_name = args[0]
    max_mix_depth = options['mixdepthsrc'] + options['mixdepthcount']
    wallet_path = get_wallet_path(wallet_name, None)
    wallet = open_test_wallet_maybe(wallet_path, wallet_name, max_mix_depth)
    if jm_single().config.get("BLOCKCHAIN",
                              "blockchain_source") == "electrum-server":
        jm_single().bc_interface.synctype = "with-script"
    while not jm_single().bc_interface.wallet_synced:
        sync_wallet(wallet, fast=options['fastsync'])

    maxcjfee = get_max_cj_fee_values(jm_single().config, options_org)
    log.info("Using maximum coinjoin fee limits per maker of {:.4%}, {} sat"
             .format(*maxcjfee))

    #Parse options and generate schedule
    #Output information to log files
    jm_single().mincjamount = options['mincjamount']
    destaddrs = args[1:]
    jmprint("Destination addresses: " + str(destaddrs), "important")
    #If the --restart flag is set we read the schedule
    #from the file, and filter out entries that are
    #already complete
    if options['restart']:
        res, schedule = get_schedule(os.path.join(logsdir,
                                                  options['schedulefile']))
        if not res:
            jmprint("Failed to load schedule, name: " + str(
                options['schedulefile']), "error")
            jmprint("Error was: " + str(schedule), "error")
            sys.exit(0)
        #This removes all entries that are marked as done
        schedule = [s for s in schedule if s[5] != 1]
        # remaining destination addresses must be stored in Taker.tdestaddrs
        # in case of tweaks; note we can't change, so any passed on command
        # line must be ignored:
        if len(destaddrs) > 0:
            jmprint("For restarts, destinations are taken from schedule file,"
                    " so passed destinations on the command line were ignored.",
                    "important")
            if input("OK? (y/n)") != "y":
                sys.exit(0)
        destaddrs = [s[3] for s in schedule if s[3] not in ["INTERNAL",
            "INTERNAL-SAME-MIXDEPTH", "addrask"]]
        jmprint("Remaining destination addresses in restart: " + ",".join(destaddrs),
                "important")
        if isinstance(schedule[0][5], str) and len(schedule[0][5]) == 64:
            #ensure last transaction is confirmed before restart
            tumble_log.info("WAITING TO RESTART...")
            txid = schedule[0][5]
            restart_waiter(txid)
            #remove the already-done entry (this connects to the other TODO,
            #probably better *not* to truncate the done-already txs from file,
            #but simplest for now.
            schedule = schedule[1:]
        elif schedule[0][5] != 0:
            print("Error: first schedule entry is invalid.")
            sys.exit(0)
        with open(os.path.join(logsdir, options['schedulefile']), "wb") as f:
                    f.write(schedule_to_text(schedule))
        tumble_log.info("TUMBLE RESTARTING")
    else:
        #Create a new schedule from scratch
        schedule = get_tumble_schedule(options, destaddrs)
        tumble_log.info("TUMBLE STARTING")
        with open(os.path.join(logsdir, options['schedulefile']), "wb") as f:
            f.write(schedule_to_text(schedule))
        print("Schedule written to logs/" + options['schedulefile'])
    tumble_log.info("With this schedule: ")
    tumble_log.info(pprint.pformat(schedule))

    # Dynamically estimate an expected tx fee for the whole tumbling run.
    # This is very rough: we guess with 2 inputs and 2 outputs each.
    if options['txfee'] == -1:
        options['txfee'] = max(options['txfee'], estimate_tx_fee(2, 2,
                                        txtype="p2sh-p2wpkh"))
        log.debug("Estimated miner/tx fee for each cj participant: " + str(
            options['txfee']))
    assert (options['txfee'] >= 0)

    # From the estimated tx fees, check if the expected amount is a
    # significant value compared the the cj amount
    involved_parties = len(schedule)    # own participation in each CJ
    for item in schedule:
        involved_parties += item[2] #  number of total tumble counterparties
    total_tumble_amount = int(0)
    max_mix_to_tumble = min(options['mixdepthsrc']+options['mixdepthcount'], \
                            max_mix_depth)
    for i in range(options['mixdepthsrc'], max_mix_to_tumble):
        total_tumble_amount += wallet.get_balance_by_mixdepth()[i]
        if total_tumble_amount == 0:
            raise ValueError("No confirmed coins in the selected mixdepth(s). Quitting")
    exp_tx_fees_ratio = (involved_parties * options['txfee']) \
        / total_tumble_amount
    if exp_tx_fees_ratio > 0.05:
        jmprint('WARNING: Expected bitcoin network miner fees for the whole '
            'tumbling run are roughly {:.1%}'.format(exp_tx_fees_ratio), "warning")
        if not options['restart'] and input('You might want to modify your tx_fee'
            ' settings in joinmarket.cfg. Still continue? (y/n):')[0] != 'y':
            sys.exit('Aborted by user.')
    else:
        log.info("Estimated miner/tx fees for this coinjoin amount for the "
            "whole tumbling run: {:.1%}".format(exp_tx_fees_ratio))

    print("Progress logging to logs/TUMBLE.log")

    def filter_orders_callback(orders_fees, cjamount):
        """Decide whether to accept fees
        """
        return tumbler_filter_orders_callback(orders_fees, cjamount, taker, options)

    def taker_finished(res, fromtx=False, waittime=0.0, txdetails=None):
        """on_finished_callback for tumbler; processing is almost entirely
        deferred to generic taker_finished in tumbler_support module, except
        here reactor signalling.
        """
        sfile = os.path.join(logsdir, options['schedulefile'])
        tumbler_taker_finished_update(taker, sfile, tumble_log, options,
                                      res, fromtx, waittime, txdetails)
        if not fromtx:
            reactor.stop()
        elif fromtx != "unconfirmed":
            reactor.callLater(waittime*60, clientfactory.getClient().clientStart)

    #instantiate Taker with given schedule and run
    taker = Taker(wallet,
                  schedule,
                  order_chooser=options['order_choose_fn'],
                  max_cj_fee=maxcjfee,
                  callbacks=(filter_orders_callback, None, taker_finished),
                  tdestaddrs=destaddrs)
    clientfactory = JMClientProtocolFactory(taker)
    nodaemon = jm_single().config.getint("DAEMON", "no_daemon")
    daemon = True if nodaemon == 1 else False
    if jm_single().config.get("BLOCKCHAIN", "network") in ["regtest", "testnet"]:
        startLogging(sys.stdout)
    start_reactor(jm_single().config.get("DAEMON", "daemon_host"),
                  jm_single().config.getint("DAEMON", "daemon_port"),
                  clientfactory, daemon=daemon)

if __name__ == "__main__":
    main()
    print('done')

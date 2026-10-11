Eventyay Business
==========================

This is a plugin for `eventyay`_. 

Eventyay plugin for tiers, add-ons, and billing restrictions

Development setup
-----------------

1. Make sure that you have a working `eventyay development setup`_.

2. Clone this repository.

3. Activate the virtual environment you use for eventyay development.

4. Execute ``python setup.py develop`` within this directory to register this application with eventyay's plugin registry.

5. Execute ``make`` within this directory to compile translations.

6. Restart your local eventyay server. You can now use the plugin from this repository for your events by enabling it in
   the 'plugins' tab in the settings.

Upgrade notes for platform fees
-------------------------------

Platform ticket fees are now configured by this plugin. The former core global
``ticket_fee_percentage`` and ``ticket_fee_maximum`` settings are no longer read.
For an event without a matching country and currency override, the plugin uses
the active tier's ``commerce.platform_fee_percent`` entitlement. If there is no
such entitlement, the percentage is zero. Without a country override, there is
no maximum fee cap.

Before upgrading a fee-bearing installation, configure country overrides or
publish tier versions with the intended fee entitlement and assign organizers
to those tiers. Review existing global fee settings and reproduce the intended
rates in the plugin before creating new fee-bearing orders. Old global values
are not migrated automatically, and fees missed during the transition are not
recovered by later configuration changes.

This plugin has CI set up to enforce a few code style rules. To check locally, you need these packages installed::

    pip install flake8 isort black

To check your plugin for rule violations, run::

    black --check .
    isort -c .
    flake8 .

You can auto-fix some of these issues by running::

    isort .
    black .

To automatically check for these issues before you commit, you can run ``sh .install-hooks.sh``.


License
-------


Copyright 2026 eventyay team

Released under the terms of the Apache License 2.0



.. _eventyay: https://github.com/eventyay/eventyay
.. _eventyay development setup: https://docs.eventyay.eu/en/latest/development/setup.html

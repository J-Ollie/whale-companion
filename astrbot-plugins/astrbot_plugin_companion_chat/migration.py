"""Pure configuration mapping; credentials and runtime state never enter this map."""


def merge_configs(configs):
    output = {}
    for section in ('active', 'batch', 'friends', 'breeze'):
        for key, value in configs[section].items():
            if key in ('owner_qq', 'platform_id', 'bot_qq'):
                if key in output and output[key] != value:
                    raise ValueError('Conflicting shared identity: ' + key)
                output[key] = value
            else:
                output[section + '_' + key] = value
    output['maintenance_mode'] = False
    return output

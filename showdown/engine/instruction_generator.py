import constants
from copy import copy
from config import logger

from showdown.damage_calculator import damage_multipication_array
from showdown.damage_calculator import pokemon_type_indicies
from showdown.helpers import accuracy_multiplier_lookup

from .special_effects.abilities.on_switch_in import ability_on_switch_in
from .special_effects.items.end_of_turn import item_end_of_turn
from .special_effects.abilities.end_of_turn import ability_end_of_turn


possible_affected_strings = {
    constants.SELF: constants.OPPONENT,
    constants.OPPONENT: constants.SELF
}


same_side_strings = [
    constants.SELF,
    constants.ALLY_SIDE
]


opposing_side_strings = [
    constants.NORMAL,
    constants.OPPONENT,
    constants.FOESIDE,
    constants.ALL_ADJACENT_FOES,
    constants.ALL_ADJACENT,
    constants.ALL,
]


weather_instructions = {
    constants.SUN,
    constants.RAIN,
    constants.SAND,
    constants.HAIL,
}

SPECIAL_LOGIC_MOVES = {
    constants.SUN,
    constants.RAIN,
    constants.SAND,
    constants.HAIL,
    constants.TRICK_ROOM,
}


def get_instructions_from_special_logic_move(mutator, move_name, instructions):
    if instructions.frozen:
        return [instructions]

    new_instructions = list()
    mutator.apply(instructions.instructions)
    if move_name in weather_instructions and mutator.state.weather != move_name and mutator.state.weather not in constants.IRREVERSIBLE_WEATHER:
        new_instructions.append(
            (constants.MUTATOR_WEATHER_START, move_name, mutator.state.weather)
        )
    elif move_name == constants.TRICK_ROOM:
        new_instructions.append(
            (constants.MUTATOR_TOGGLE_TRICKROOM,)
        )

    mutator.reverse(instructions.instructions)

    for i in new_instructions:
        instructions.add_instruction(i)

    return [instructions]


def get_state_from_volatile_status(mutator, volatile_status, attacker, affected_side, first_move, instruction):
    if instruction.frozen or not volatile_status:
        return [instruction]

    if affected_side in same_side_strings:
        affected_side = attacker
    elif affected_side in opposing_side_strings:
        affected_side = possible_affected_strings[attacker]
    else:
        logger.critical("Invalid affected_side: {}".format(affected_side))
        return [instruction]

    side = get_side_from_state(mutator.state, affected_side)
    mutator.apply(instruction.instructions)
    if volatile_status in side.active.volatile_status:
        mutator.reverse(instruction.instructions)
        return [instruction]

    if can_be_volatile_statused(side, volatile_status, first_move) and volatile_status not in side.active.volatile_status:
        apply_status_instruction = (
            constants.MUTATOR_APPLY_VOLATILE_STATUS,
            affected_side,
            volatile_status
        )
        mutator.reverse(instruction.instructions)
        instruction.add_instruction(apply_status_instruction)
        if volatile_status == constants.SUBSTITUTE:
            instruction.add_instruction(
                (
                    constants.MUTATOR_DAMAGE,
                    affected_side,
                    side.active.maxhp * 0.25
                )
            )
    else:
        mutator.reverse(instruction.instructions)

    return [instruction]


def get_instructions_from_switch(mutator, attacker, switch_pokemon_name, instructions):
    if attacker not in possible_affected_strings:
        raise ValueError("attacker parameter must be one of: {}".format(', '.join(possible_affected_strings)))

    attacking_side = get_side_from_state(mutator.state, attacker)
    defending_side = get_side_from_state(mutator.state, possible_affected_strings[attacker])
    mutator.apply(instructions.instructions)
    instruction_additions = remove_volatile_status_and_boosts_instructions(attacking_side, attacker)

    for move in filter(lambda x: x[constants.DISABLED] is True and x[constants.CURRENT_PP], attacking_side.active.moves):
        instruction_additions.append(
            (
                constants.MUTATOR_ENABLE_MOVE,
                attacker,
                move[constants.ID]
            )
        )

    if attacking_side.active.ability == 'regenerator' and attacking_side.active.hp:
        hp_missing = attacking_side.active.maxhp - attacking_side.active.hp
        instruction_additions.append(
            (
                constants.MUTATOR_HEAL,
                attacker,
                int(min(1 / 3 * attacking_side.active.maxhp, hp_missing))
            )
        )
    elif attacking_side.active.ability == 'naturalcure' and attacking_side.active.status is not None:
        instruction_additions.append(
            (
                constants.MUTATOR_REMOVE_STATUS,
                attacker,
                attacking_side.active.status
            )
        )

    instruction_additions.append(
        (
            constants.MUTATOR_SWITCH,
            attacker,
            attacking_side.active.id,
            switch_pokemon_name
        )
    )

    switch_pkmn = attacking_side.reserve[switch_pokemon_name]
    attacking_pokemon = attacking_side.active
    # account for stealth rock damage
    if attacking_side.side_conditions[constants.STEALTH_ROCK] == 1:
        multiplier = 1
        rock_type_index = pokemon_type_indicies['rock']
        for pkmn_type in switch_pkmn.types:
            multiplier *= damage_multipication_array[rock_type_index][pokemon_type_indicies[pkmn_type]]

        instruction_additions.append(
            (
                constants.MUTATOR_DAMAGE,
                attacker,
                min(1 / 8 * multiplier * switch_pkmn.maxhp, switch_pkmn.hp)
            )
        )

    # account for spikes damage
    if attacking_side.side_conditions[constants.SPIKES] > 0 and switch_pkmn.is_grounded():
        spike_count = attacking_side.side_conditions[constants.SPIKES]
        instruction_additions.append(
            (
                constants.MUTATOR_DAMAGE,
                attacker,
                min(1 / 8 * spike_count * switch_pkmn.maxhp, switch_pkmn.hp)
            )
        )

    # account for stickyweb speed drop
    if attacking_side.side_conditions[constants.STICKY_WEB] == 1 and switch_pkmn.is_grounded():
        instruction_additions.append(
            (
                constants.MUTATOR_UNBOOST,
                attacker,
                constants.SPEED,
                1
            )
        )

    # account for toxic spikes effect
    if attacking_side.side_conditions[constants.TOXIC_SPIKES] >= 1 and switch_pkmn.is_grounded():
        if not immune_to_status(mutator.state, switch_pkmn, attacking_pokemon, constants.POISON):
            if attacking_side.side_conditions[constants.TOXIC_SPIKES] == 1:
                instruction_additions.append(
                    (
                        constants.MUTATOR_APPLY_STATUS,
                        attacker,
                        constants.POISON
                    )
                )
            elif attacking_side.side_conditions[constants.TOXIC_SPIKES] == 2:
                instruction_additions.append(
                    (
                        constants.MUTATOR_APPLY_STATUS,
                        attacker,
                        constants.TOXIC
                    )
                )
        elif 'poison' in switch_pkmn.types:
            instruction_additions.append(
                (
                    constants.MUTATOR_SIDE_END,
                    attacker,
                    constants.TOXIC_SPIKES,
                    attacking_side.side_conditions[constants.TOXIC_SPIKES]
                )
            )

    # account for switch-in abilities
    ability_switch_in_instruction = ability_on_switch_in(
        switch_pkmn.ability,
        mutator.state,
        attacker,
        attacking_side.active,
        possible_affected_strings[attacker],
        defending_side.active
    )
    if ability_switch_in_instruction is not None:
        instruction_additions.append(
            ability_switch_in_instruction
        )

    mutator.reverse(instructions.instructions)
    for i in instruction_additions:
        instructions.add_instruction(i)

    return [instructions]


def get_instructions_from_flinched(mutator, attacker, instruction):
    """If the attacker has been flinched, freeze the state so that nothing happens"""
    if attacker not in possible_affected_strings:
        raise ValueError("attacker parameter must be one of: {}".format(', '.join(possible_affected_strings)))

    mutator.apply(instruction.instructions)

    side = get_side_from_state(mutator.state, attacker)
    if constants.FLINCH in side.active.volatile_status:
        remove_flinch_instruction = (
            constants.MUTATOR_REMOVE_VOLATILE_STATUS,
            attacker,
            constants.FLINCH
        )
        mutator.reverse(instruction.instructions)
        instruction.add_instruction(remove_flinch_instruction)
        instruction.frozen = True
        return [instruction]
    else:
        mutator.reverse(instruction.instructions)
        return [instruction]


def get_instructions_from_statuses_that_freeze_the_state(mutator, attacker, defender, move, opponent_move, instruction):
    instructions = [instruction]
    attacker_side = get_side_from_state(mutator.state, attacker)
    defender_side = get_side_from_state(mutator.state, defender)

    mutator.apply(instruction.instructions)

    if constants.PARALYZED == attacker_side.active.status:
        fully_paralyzed_instruction = copy(instruction)
        fully_paralyzed_instruction.update_percentage(constants.FULLY_PARALYZED_PERCENT)
        fully_paralyzed_instruction.frozen = True
        instruction.update_percentage(1 - constants.FULLY_PARALYZED_PERCENT)
        instructions.append(fully_paralyzed_instruction)

    elif constants.SLEEP == attacker_side.active.status:
        still_asleep_instruction = copy(instruction)
        still_asleep_instruction.update_percentage(1 - constants.WAKE_UP_PERCENT)
        still_asleep_instruction.frozen = True
        instruction.update_percentage(constants.WAKE_UP_PERCENT)
        instruction.add_instruction(
            (
                constants.MUTATOR_REMOVE_STATUS,
                attacker,
                constants.SLEEP
            )
        )
        instructions.append(still_asleep_instruction)

    elif constants.FROZEN == attacker_side.active.status:
        still_frozen_instruction = copy(instruction)
        instruction.add_instruction(
            (
                constants.MUTATOR_REMOVE_STATUS,
                attacker,
                constants.FROZEN
            )
        )
        if move[constants.ID] not in constants.THAW_IF_USES and opponent_move.get(constants.ID) not in constants.THAW_IF_HIT_BY and opponent_move.get(constants.TYPE) != 'fire':
            still_frozen_instruction.update_percentage(1 - constants.THAW_PERCENT)
            still_frozen_instruction.frozen = True
            instruction.update_percentage(constants.THAW_PERCENT)
            instructions.append(still_frozen_instruction)

    if constants.POWDER in move[constants.FLAGS] and ('grass' in defender_side.active.types or defender_side.active.ability == 'overcoat'):
        instruction.frozen = True

    mutator.reverse(instruction.instructions)

    return instructions


def get_states_from_damage(mutator, defender, damage, accuracy, attacking_move, instruction):
    """Given state, generate multiple states based on all of the possible damage combinations
       This versions assumes that all damage deals a constant amount
       The different states are based on whether or not the attack misses

       To make this deal with multiple potential damage rolls, change `damage` to a list and iterate over it
       """

    attacker = possible_affected_strings[defender]
    attacker_side = get_side_from_state(mutator.state, attacker)
    damage_side = get_side_from_state(mutator.state, defender)

    # `damage is None` means that the move does not deal damage
    # for example, will-o-wisp
    if instruction.frozen or damage is None:
        return [instruction]

    crash = attacking_move.get(constants.CRASH)
    recoil = attacking_move.get(constants.RECOIL)
    drain = attacking_move.get(constants.DRAIN)
    move_flags = attacking_move.get(constants.FLAGS, {})

    mutator.apply(instruction.instructions)

    if accuracy is True:
        accuracy = 100
    else:
        accuracy = min(100, accuracy * accuracy_multiplier_lookup[attacker_side.active.accuracy_boost] / accuracy_multiplier_lookup[damage_side.active.evasion_boost])
    percent_hit = accuracy / 100

    # `damage == 0` means that the move deals damage, but not in this situation
    # for example: using Return against a Ghost-type
    # the state must be frozen because any secondary effects must not take place
    if damage == 0:
        if crash:
            crash_percent = crash[0] / crash[1]
            crash_instruction = (
                constants.MUTATOR_DAMAGE,
                attacker,
                min(int(crash_percent * attacker_side.active.maxhp), attacker_side.active.hp)
            )
            mutator.reverse(instruction.instructions)
            instruction.add_instruction(crash_instruction)
        else:
            mutator.reverse(instruction.instructions)
        instruction.frozen = True
        return [instruction]

    if defender not in possible_affected_strings:
        raise ValueError("attacker parameter must be one of: {}".format(', '.join(possible_affected_strings)))

    instructions = []
    instruction_additions = []
    move_missed_instruction = copy(instruction)
    if percent_hit > 0:
        if constants.SUBSTITUTE in damage_side.active.volatile_status and constants.SOUND not in move_flags:
            if damage >= damage_side.active.maxhp * 0.25:
                actual_damage = damage_side.active.maxhp * 0.25
                instruction_additions.append(
                    (
                        constants.MUTATOR_REMOVE_VOLATILE_STATUS,
                        defender,
                        constants.SUBSTITUTE
                    )
                )
            else:
                actual_damage = damage
        else:
            # dont drop hp below 0 (min() statement), and dont overheal (max() statement)
            actual_damage = max(min(damage, damage_side.active.hp), -1*(damage_side.active.maxhp - damage_side.active.hp))

            if damage_side.active.ability == 'sturdy' and damage_side.active.hp == damage_side.active.maxhp:
                actual_damage -= 1

            instruction_additions.append(
                (
                    constants.MUTATOR_DAMAGE,
                    defender,
                    actual_damage
                )
            )
        instruction.update_percentage(percent_hit)

        if damage_side.active.hp <= 0:
            instruction.frozen = True

        if drain:
            drain_percent = drain[0] / drain[1]
            drain_instruction = (
                constants.MUTATOR_HEAL,
                attacker,
                min(int(drain_percent * actual_damage), int(attacker_side.active.maxhp - attacker_side.active.hp))
            )
            instruction_additions.append(drain_instruction)
        if recoil:
            recoil_percent = recoil[0] / recoil[1]
            recoil_instruction = (
                constants.MUTATOR_DAMAGE,
                attacker,
                min(int(recoil_percent * actual_damage), int(attacker_side.active.hp))
            )
            instruction_additions.append(recoil_instruction)

        instructions.append(instruction)

    if percent_hit < 1:
        move_missed_instruction.frozen = True
        move_missed_instruction.update_percentage(1 - percent_hit)
        if crash:
            crash_percent = crash[0] / crash[1]
            crash_instruction = (
                constants.MUTATOR_DAMAGE,
                attacker,
                min(int(crash_percent * attacker_side.active.maxhp), attacker_side.active.hp)
            )
            move_missed_instruction.add_instruction(crash_instruction)

        instructions.append(move_missed_instruction)

    mutator.reverse(instruction.instructions)
    for i in instruction_additions:
        instruction.add_instruction(i)

    return instructions


def get_instructions_from_side_conditions(mutator, attacker_string, side_string, condition, instruction):
    if instruction.frozen:
        return [instruction]

    if attacker_string not in possible_affected_strings:
        raise ValueError("attacker parameter must be one of: {}".format(', '.join(possible_affected_strings)))

    if side_string in same_side_strings:
        side_string = attacker_string
    elif side_string in opposing_side_strings:
        side_string = possible_affected_strings[attacker_string]
    else:
        raise ValueError("Invalid Side String: {}".format(side_string))

    instruction_additions = []
    side = get_side_from_state(mutator.state, side_string)
    mutator.apply(instruction.instructions)
    if condition == constants.SPIKES:
        max_layers = 3
    elif condition == constants.TOXIC_SPIKES:
        max_layers = 2
    else:
        max_layers = 1

    if side.side_conditions[condition] < max_layers:
        instruction_additions.append(
            (
                constants.MUTATOR_SIDE_START,
                side_string,
                condition,
                1
            )
        )
    mutator.reverse(instruction.instructions)
    for i in instruction_additions:
        instruction.add_instruction(i)

    return [instruction]


def get_instructions_from_hazard_clearing_moves(mutator, attacker_string, move, instruction):
    if instruction.frozen:
        return [instruction]

    if attacker_string not in possible_affected_strings:
        raise ValueError("attacker parameter must be one of: {}".format(', '.join(possible_affected_strings)))

    defender_string = possible_affected_strings[attacker_string]

    instruction_additions = []
    mutator.apply(instruction.instructions)

    attacker_side = get_side_from_state(mutator.state, attacker_string)
    defender_side = get_side_from_state(mutator.state, defender_string)

    if move[constants.ID] == 'defog':
        for side_condition, amount in attacker_side.side_conditions.items():
            if amount > 0 and side_condition in constants.DEFOG_CLEARS:
                instruction_additions.append(
                    (
                        constants.MUTATOR_SIDE_END,
                        attacker_string,
                        side_condition,
                        amount
                    )
                )
        for side_condition, amount in defender_side.side_conditions.items():
            if amount > 0 and side_condition in constants.DEFOG_CLEARS:
                instruction_additions.append(
                    (
                        constants.MUTATOR_SIDE_END,
                        defender_string,
                        side_condition,
                        amount
                    )
                )

    # ghost-type misses are dealt with by freezing the state. i.e. this elif will not be reached if the move missed
    elif move[constants.ID] == 'rapidspin':
        side = get_side_from_state(mutator.state, attacker_string)
        for side_condition, amount in side.side_conditions.items():
            if amount > 0 and side_condition in constants.RAPID_SPIN_CLEARS:
                instruction_additions.append(
                    (
                        constants.MUTATOR_SIDE_END,
                        attacker_string,
                        side_condition,
                        amount
                    )
                )
    else:
        raise ValueError("{} is not a hazard clearing move".format(move[constants.ID]))

    mutator.reverse(instruction.instructions)
    for i in instruction_additions:
        instruction.add_instruction(i)

    return [instruction]


def get_states_from_status_effects(mutator, defender, status, accuracy, instruction):
    """Returns the possible states from status effects"""
    if instruction.frozen or status is None:
        return [instruction]

    if defender not in possible_affected_strings:
        raise ValueError("attacker parameter must be one of: {}".format(', '.join(possible_affected_strings)))

    instructions = []
    if accuracy is True:
        accuracy = 100
    percent_hit = accuracy / 100

    mutator.apply(instruction.instructions)
    instruction_additions = []
    defending_side = get_side_from_state(mutator.state, defender)
    attacking_side = get_side_from_state(mutator.state, possible_affected_strings[defender])

    if sleep_clause_activated(defending_side, status):
        mutator.reverse(instruction.instructions)
        return [instruction]

    if immune_to_status(mutator.state, defending_side.active, attacking_side.active, status):
        mutator.reverse(instruction.instructions)
        return [instruction]

    move_missed_instruction = copy(instruction)
    if percent_hit > 0:
        move_hit_instruction = (
            constants.MUTATOR_APPLY_STATUS,
            defender,
            status
        )

        instruction_additions.append(move_hit_instruction)
        instruction.update_percentage(percent_hit)
        instructions.append(instruction)

    if percent_hit < 1:
        move_missed_instruction.frozen = True
        move_missed_instruction.update_percentage(1 - percent_hit)
        instructions.append(move_missed_instruction)

    mutator.reverse(instruction.instructions)
    for i in instruction_additions:
        instruction.add_instruction(i)

    return instructions


def get_states_from_boosts(mutator, side_string, boosts, accuracy, instruction):
    if instruction.frozen or not boosts:
        return [instruction]

    if side_string not in possible_affected_strings:
        raise ValueError("attacker parameter must be one of: {}. Value: {}".format(
            ', '.join(possible_affected_strings),
            side_string
        )
        )

    instructions = []
    if accuracy is True:
        accuracy = 100
    percent_hit = accuracy / 100

    mutator.apply(instruction.instructions)
    instruction_additions = []

    move_missed_instruction = copy(instruction)
    side = get_side_from_state(mutator.state, side_string)
    if percent_hit > 0:
        for k, v in boosts.items():
            pkmn_boost = get_boost_from_boost_string(side, k)
            if v > 0:
                new_boost = pkmn_boost + v
                if new_boost > constants.MAX_BOOSTS:
                    new_boost = constants.MAX_BOOSTS
                boost_instruction = (
                    constants.MUTATOR_BOOST,
                    side_string,
                    k,
                    new_boost - pkmn_boost
                )
            else:
                new_boost = pkmn_boost + v
                if new_boost < -1 * constants.MAX_BOOSTS:
                    new_boost = -1 * constants.MAX_BOOSTS
                boost_instruction = (
                    constants.MUTATOR_BOOST,
                    side_string,
                    k,
                    new_boost - pkmn_boost
                )
            instruction_additions.append(boost_instruction)

        instruction.update_percentage(percent_hit)
        instructions.append(instruction)

    if percent_hit < 1:
        move_missed_instruction.update_percentage(1 - percent_hit)
        instructions.append(move_missed_instruction)

    mutator.reverse(instruction.instructions)
    for i in instruction_additions:
        instruction.add_instruction(i)

    return instructions


def get_states_from_flinching_moves(defender, accuracy, first_move, instruction):
    if instruction.frozen or not first_move:
        return [instruction]

    if defender not in possible_affected_strings:
        raise ValueError("attacker parameter must be one of: {}".format(', '.join(possible_affected_strings)))

    instructions = []
    if accuracy is True:
        accuracy = 100
    percent_hit = accuracy / 100

    if percent_hit > 0:
        flinched_instruction = copy(instruction)
        flinch_mutator_instruction = (
            constants.MUTATOR_APPLY_VOLATILE_STATUS,
            defender,
            constants.FLINCH
        )
        flinched_instruction.add_instruction(flinch_mutator_instruction)
        flinched_instruction.update_percentage(percent_hit)
        instructions.append(flinched_instruction)

    if percent_hit < 1:
        instruction.update_percentage(1 - percent_hit)
        instructions.append(instruction)

    return instructions


def get_state_from_attacker_recovery(mutator, attacker_string, move, instruction):
    if instruction.frozen:
        return [instruction]

    mutator.apply(instruction.instructions)

    target = move[constants.HEAL_TARGET]
    if target in opposing_side_strings:
        side_string = possible_affected_strings[attacker_string]
    else:
        side_string = attacker_string

    pkmn = get_side_from_state(mutator.state, side_string).active
    try:
        health_recovered = float(move[constants.HEAL][0] / move[constants.HEAL][1]) * pkmn.maxhp
    except KeyError:
        health_recovered = 0

    if health_recovered == 0:
        mutator.reverse(instruction.instructions)
        return [instruction]

    final_health = pkmn.hp + health_recovered
    if final_health > pkmn.maxhp:
        health_recovered -= (final_health - pkmn.maxhp)
    elif final_health < 0:
        health_recovered -= final_health

    heal_instruction = (
        constants.MUTATOR_HEAL,
        side_string,
        health_recovered
    )

    mutator.reverse(instruction.instructions)

    if health_recovered:
        instruction.add_instruction(heal_instruction)

    return [instruction]


def get_end_of_turn_instructions(mutator, instruction, bot_move, opponent_move, bot_moves_first):
    # determine which goes first
    if bot_moves_first:
        sides = [constants.SELF, constants.OPPONENT]
    else:
        sides = [constants.OPPONENT, constants.SELF]

    mutator.apply(instruction.instructions)

    # weather damage - sand and hail
    for attacker in sides:
        side = get_side_from_state(mutator.state, attacker)
        pkmn = side.active

        if pkmn.ability == 'magicguard' or not pkmn.hp:
            continue

        if mutator.state.weather == constants.SAND and not any(t in pkmn.types for t in ['steel', 'rock', 'ground']):
            sand_damage_instruction = (
                constants.MUTATOR_DAMAGE,
                attacker,
                max(0, int(min(pkmn.maxhp * 0.0625, pkmn.hp)))
            )
            mutator.apply_one(sand_damage_instruction)
            instruction.add_instruction(sand_damage_instruction)

        elif mutator.state.weather == constants.HAIL and 'ice' not in pkmn.types:
            ice_damage_instruction = (
                constants.MUTATOR_DAMAGE,
                attacker,
                max(0, int(min(pkmn.maxhp * 0.0625, pkmn.hp)))
            )
            mutator.apply_one(ice_damage_instruction)
            instruction.add_instruction(ice_damage_instruction)

    # item and ability - they can add one instruction each
    for attacker in sides:
        defender = possible_affected_strings[attacker]
        side = get_side_from_state(mutator.state, attacker)
        defending_side = get_side_from_state(mutator.state, defender)
        pkmn = side.active
        defending_pkmn = defending_side.active

        item_instruction = item_end_of_turn(side.active.item, mutator.state, attacker, pkmn, defender, defending_pkmn)
        if item_instruction is not None:
            mutator.apply_one(item_instruction)
            instruction.add_instruction(item_instruction)

        ability_instruction = ability_end_of_turn(side.active.ability, mutator.state, attacker, pkmn, defender, defending_pkmn)
        if ability_instruction is not None:
            mutator.apply_one(ability_instruction)
            instruction.add_instruction(ability_instruction)

    # poison, toxic, and burn damage
    for attacker in sides:
        side = get_side_from_state(mutator.state, attacker)
        pkmn = side.active

        if pkmn.ability == 'magicguard' or not pkmn.hp:
            continue

        if constants.TOXIC == pkmn.status and pkmn.ability != 'poisonheal':
            toxic_count = side.side_conditions[constants.TOXIC_COUNT]
            toxic_multiplier = (1 / 16) * toxic_count + (1 / 16)
            toxic_damage = max(0, int(min(pkmn.maxhp * toxic_multiplier, pkmn.hp)))

            toxic_damage_instruction = (
                constants.MUTATOR_DAMAGE,
                attacker,
                toxic_damage
            )
            toxic_count_instruction = (
                constants.MUTATOR_SIDE_START,
                attacker,
                constants.TOXIC_COUNT,
                1
            )
            mutator.apply_one(toxic_damage_instruction)
            mutator.apply_one(toxic_count_instruction)

            instruction.add_instruction(toxic_damage_instruction)
            instruction.add_instruction(toxic_count_instruction)

        elif constants.BURN == pkmn.status:
            burn_damage_instruction = (
                constants.MUTATOR_DAMAGE,
                attacker,
                max(0, int(min(pkmn.maxhp * 0.0625, pkmn.hp)))
            )
            mutator.apply_one(burn_damage_instruction)
            instruction.add_instruction(burn_damage_instruction)

        elif constants.POISON == pkmn.status and pkmn.ability != 'poisonheal':
            poison_damage_instruction = (
                constants.MUTATOR_DAMAGE,
                attacker,
                max(0, int(min(pkmn.maxhp * 0.125, pkmn.hp)))
            )
            mutator.apply_one(poison_damage_instruction)
            instruction.add_instruction(poison_damage_instruction)

    # leechseed sap damage
    for attacker in sides:
        defender = possible_affected_strings[attacker]
        side = get_side_from_state(mutator.state, attacker)
        defending_side = get_side_from_state(mutator.state, defender)
        pkmn = side.active
        defending_pkmn = defending_side.active

        if pkmn.ability == 'magicguard' or not pkmn.hp or not defending_pkmn.hp:
            continue

        if constants.LEECH_SEED in pkmn.volatile_status:
            # damage taken
            damage_sapped = max(0, int(min(pkmn.maxhp * 0.125, pkmn.hp)))
            sap_instruction = (
                constants.MUTATOR_DAMAGE,
                attacker,
                damage_sapped
            )

            # heal amount
            damage_from_full = defending_pkmn.maxhp - defending_pkmn.hp
            heal_instruction = (
                constants.MUTATOR_HEAL,
                defender,
                min(damage_sapped, damage_from_full)
            )

            mutator.apply_one(sap_instruction)
            mutator.apply_one(heal_instruction)
            instruction.add_instruction(sap_instruction)
            instruction.add_instruction(heal_instruction)

    # volatile-statuses
    for attacker in sides:
        side = get_side_from_state(mutator.state, attacker)
        pkmn = side.active

        if any(vs in constants.PROTECT_VOLATILE_STATUSES for vs in pkmn.volatile_status):
            if constants.PROTECT in pkmn.volatile_status:
                volatile_status_to_remove = constants.PROTECT
            elif constants.BANEFUL_BUNKER in pkmn.volatile_status:
                volatile_status_to_remove = constants.BANEFUL_BUNKER
            elif constants.SPIKY_SHIELD in pkmn.volatile_status:
                volatile_status_to_remove = constants.SPIKY_SHIELD
            else:
                # should never happen
                raise Exception("Pokemon has volatile status that is not caught here: {}".format(pkmn.volatile_status))

            remove_protect_volatile_status_instruction = (
                constants.MUTATOR_REMOVE_VOLATILE_STATUS,
                attacker,
                volatile_status_to_remove
            )
            start_protect_side_condition_instruction = (
                    constants.MUTATOR_SIDE_START,
                    attacker,
                    constants.PROTECT,
                    1
            )
            mutator.apply_one(remove_protect_volatile_status_instruction)
            mutator.apply_one(start_protect_side_condition_instruction)
            instruction.add_instruction(remove_protect_volatile_status_instruction)
            instruction.add_instruction(start_protect_side_condition_instruction)

        elif side.side_conditions[constants.PROTECT]:
            end_protect_side_condition_instruction = (
                constants.MUTATOR_SIDE_END,
                attacker,
                constants.PROTECT,
                side.side_conditions[constants.PROTECT]
            )
            mutator.apply_one(end_protect_side_condition_instruction)
            instruction.add_instruction(end_protect_side_condition_instruction)

        if constants.ROOST in pkmn.volatile_status:
            remove_roost_instruction = (
                constants.MUTATOR_REMOVE_VOLATILE_STATUS,
                attacker,
                constants.ROOST,
            )
            mutator.apply_one(remove_roost_instruction)
            instruction.add_instruction(remove_roost_instruction)

    # disable not used moves if choice-item is held
    for attacker in sides:
        side = get_side_from_state(mutator.state, attacker)
        pkmn = side.active

        if attacker == constants.SELF:
            move = bot_move
        else:
            move = opponent_move

        try:
            locking_move = move[constants.SELF][constants.VOLATILE_STATUS] == constants.LOCKED_MOVE
        except KeyError:
            locking_move = False

        if constants.SWITCH_STRING not in move and (pkmn.item in constants.CHOICE_ITEMS or locking_move):
            move_used = move[constants.ID]
            for m in filter(lambda x: x[constants.ID] != move_used and not x[constants.DISABLED], pkmn.moves):
                disable_instruction = (
                    constants.MUTATOR_DISABLE_MOVE,
                    attacker,
                    m[constants.ID]
                )
                mutator.apply_one(disable_instruction)
                instruction.add_instruction(disable_instruction)

    mutator.reverse(instruction.instructions)

    return [instruction]


def get_state_from_drag(mutator, attacking_move, attacking_side_string, move_target, instruction):
    if constants.DRAG not in attacking_move[constants.FLAGS] or instruction.frozen:
        return [instruction]

    if move_target in same_side_strings:
        affected_side = get_side_from_state(mutator.state, attacking_side_string)
        affected_side_string = attacking_side_string
    elif move_target in opposing_side_strings:
        affected_side = get_side_from_state(mutator.state, possible_affected_strings[attacking_side_string])
        affected_side_string = possible_affected_strings[attacking_side_string]
    else:
        raise ValueError("Invalid value for move_target: {}".format(move_target))

    mutator.apply(instruction.instructions)
    new_instructions = remove_volatile_status_and_boosts_instructions(affected_side, affected_side_string)
    mutator.reverse(instruction.instructions)

    for i in new_instructions:
        instruction.add_instruction(i)

    return [instruction]


def remove_volatile_status_and_boosts_instructions(side, side_string):
    instruction_additions = []
    for v_status in side.active.volatile_status:
        instruction_additions.append(
            (
                constants.MUTATOR_REMOVE_VOLATILE_STATUS,
                side_string,
                v_status
            )
        )
    if side.side_conditions[constants.TOXIC_COUNT]:
        instruction_additions.append(
            (
                constants.MUTATOR_SIDE_END,
                side_string,
                constants.TOXIC_COUNT,
                side.side_conditions[constants.TOXIC_COUNT]
            ))
    if side.active.attack_boost:
        instruction_additions.append(
            (
                constants.MUTATOR_UNBOOST,
                side_string,
                constants.ATTACK,
                side.active.attack_boost
            ))
    if side.active.defense_boost:
        instruction_additions.append(
            (
                constants.MUTATOR_UNBOOST,
                side_string,
                constants.DEFENSE,
                side.active.defense_boost
            ))
    if side.active.special_attack_boost:
        instruction_additions.append(
            (
                constants.MUTATOR_UNBOOST,
                side_string,
                constants.SPECIAL_ATTACK,
                side.active.special_attack_boost
            ))
    if side.active.special_defense_boost:
        instruction_additions.append(
            (
                constants.MUTATOR_UNBOOST,
                side_string,
                constants.SPECIAL_DEFENSE,
                side.active.special_defense_boost
            ))
    if side.active.speed_boost:
        instruction_additions.append(
            (
                constants.MUTATOR_UNBOOST,
                side_string,
                constants.SPEED,
                side.active.speed_boost
            ))

    return instruction_additions


def get_side_from_state(state, side_string):
    if side_string == constants.SELF:
        return state.self
    elif side_string == constants.OPPONENT:
        return state.opponent
    else:
        raise ValueError("Invalid value for `side`")


def get_boost_from_boost_string(side, boost_string):
    if boost_string == constants.ATTACK:
        return side.active.attack_boost
    elif boost_string == constants.DEFENSE:
        return side.active.defense_boost
    elif boost_string == constants.SPECIAL_ATTACK:
        return side.active.special_attack_boost
    elif boost_string == constants.SPECIAL_DEFENSE:
        return side.active.special_defense_boost
    elif boost_string == constants.SPEED:
        return side.active.speed_boost
    elif boost_string == constants.ACCURACY:
        return side.active.accuracy_boost
    elif boost_string == constants.EVASION:
        return side.active.evasion_boost


def can_be_volatile_statused(side, volatile_status, first_move):
    if volatile_status in constants.PROTECT_VOLATILE_STATUSES:
        if side.side_conditions[constants.PROTECT]:
            return False
        elif first_move:
            return True
        else:
            return False
    if constants.SUBSTITUTE in side.active.volatile_status:
        return False
    if volatile_status == constants.SUBSTITUTE and side.active.hp < side.active.maxhp * 0.25:
        return False

    return True


def sleep_clause_activated(side, status):
    if status == constants.SLEEP and constants.SLEEP in [p.status for p in side.reserve.values()]:
        return True
    return False


def immune_to_status(state, defending_pkmn, attacking_pkmn, status):
    if defending_pkmn.status is not None or defending_pkmn.hp <= 0:
        return True
    if constants.SUBSTITUTE in defending_pkmn.volatile_status and attacking_pkmn.ability != 'infiltrator':
        return True
    if defending_pkmn.ability == 'shieldsdown' and ((defending_pkmn.hp / defending_pkmn.maxhp) > 0.5):
        return True
    if defending_pkmn.ability == 'comatose':
        return True
    if state.field == constants.MISTY_TERRAIN and defending_pkmn.is_grounded():
        return True

    if status == constants.FROZEN and (defending_pkmn.ability in constants.IMMUNE_TO_FROZEN_ABILITIES or 'ice' in defending_pkmn.types or state.weather == constants.DESOLATE_LAND):
        return True
    elif status == constants.BURN and ('fire' in defending_pkmn.types or defending_pkmn.ability in constants.IMMUNE_TO_BURN_ABILITIES):
        return True
    elif status == constants.SLEEP and (defending_pkmn.ability in constants.IMMUNE_TO_SLEEP_ABILITIES or state.field == constants.ELECTRIC_TERRAIN):
        return True
    elif status in [constants.POISON, constants.TOXIC] and (
            any(t in ['poison', 'steel'] for t in defending_pkmn.types) or defending_pkmn.ability in constants.IMMUNE_TO_POISON_ABILITIES):
        return True
    elif status == constants.PARALYZED and ('ground' in defending_pkmn.types or defending_pkmn.ability in constants.IMMUNE_TO_PARALYSIS_ABILITIES):
        return True

    return False

import tensorflow as tf


class Losses(object):
  def __init__(self, factory, config):
    self.config = config
    self.setup_dsl(factory, config)
    self.build_loss(config)

  def build_loss(self, config):
    # TD-error
    td_errors = self.td_error()
    square_errors = tf.square(td_errors)

    # Optimality Tightening
    if config.optimality_tightening:
      penalty, error_rescaling = self.optimality_tightening()
      square_errors = (square_errors + penalty) / error_rescaling

    # Apply bootstrap mask
    if config.bootstrapped and config.bootstrap_mask_probability < 1.0:
      td_errors *= self.bootstrap_mask
      square_errors *= self.bootstrap_mask

    # Sum bootstrap heads
    self.td_errors = tf.reduce_sum(td_errors, axis=1, name='td_errors')
    square_error = tf.reduce_sum(square_errors, axis=1, name='square_error')

    # Apply importance sampling
    self.loss = tf.reduce_mean(
        self.importance_sampling * square_error, name='loss')

    # Clip loss
    if config.loss_clipping > 0:
      self.loss = tf.maximum(
          -config.loss_clipping,
          tf.minimum(self.loss, config.loss_clipping),
          name='loss')

  # TODO Nothing handles alives yet
  # TODO How are start/end of episodes going to work?

  def td_error(self, t=0):
    target_value = self.target_value(t)
    taken_action_value = self.policy_network[t].action_value(self.action[t])
    return target_value - taken_action_value

  def target_value(self, t=0):
    return self.reward[t] + self.discount * self.value(t + 1)

  def value(self, t):
    if self.config.double_q:
      greedy_action = self.policy_network[t].greedy_actions
      return self.target_network[t].action_value(greedy_action)
    elif self.config.sarsa:
      return self.target_network[t].taken_action_value
    else:
      return self.target_network[t].values

  def persistent_advantage_target(self, t):
    q_target = self.target_value(t)
    alpha = self.config.pal_alpha
    action = self.action[t]

    advantage = self.value(t) - self.target_network[t].action_value(action)
    advantage_learning = q_target - alpha * advantage
    next_advantage = \
            self.value(t + 1) - self.target_network[t + 1].action_value(action)
    next_advantage_learning = q_target - alpha * next_advantage
    return tf.maximum(al, next_al, name='persistent_advantage_learning')

  def optimality_tightening(self):
    # Upper bounds
    upper_bounds = []
    rewards = 0
    for t in range(-1, -self.config.optimality_tightening_steps - 1, -1):
      rewards = self.reward[t] + self.discount * rewards
      q_value = self.discount**(t) * self.target_network[t].taken_action_value
      upper_bounds.append(q_value - rewards)
    upper_bound = tf.reduce_min(tf.stack(upper_bounds, axis=2), axis=2)

    upper_bound_difference = (
        self.policy_network[0].taken_action_values - upper_bound)
    upper_bound_breached = tf.to_float(upper_bound_difference > 0)
    upper_bound_penalty = tf.square(tf.nn.relu(upper_bound_difference))

    # Lower bounds
    lower_bounds = [self.total_reward[0]]
    rewards = self.reward[0]
    for t in range(1, self.config.optimality_tightening_steps + 1):
      rewards += self.reward[t] + self.discount**t
      lower_bound = rewards + self.discount**(t + 1) * self.value(t + 1)
      lower_bounds.append(lower_bound)
    lower_bound = tf.reduce_max(tf.stack(lower_bounds, axis=2), axis=2)

    lower_bound_difference = (
        lower_bound - self.policy_network[t].taken_action_values)
    lower_bound_breached = tf.to_float(lower_bound_difference > 0)
    lower_bound_penalty = tf.square(tf.nn.relu(lower_bound_difference))

    # Penalty and rescaling
    penalty = lower_bound_penalty + upper_bound_penalty
    constraints_breached = lower_bound_breached + upper_bound_breached
    error_rescaling = 1.0 / (
        1.0 + constraints_breached * self.config.optimality_penalty_ratio)

    return penalty, error_rescaling

  def actor_critic_loss(self, t, n):
    policy_loss, value_loss = 0, 0

    reward = self.policy_net.value(t + n)
    for i in range(n - 1, -1, -1):
      reward = reward * discount_rate + Reward(t + i)
      value = self.policy_net.values(t + i)
      td_error = reward - value

      log_policy = self.policy_net.log_policy(t + 1)
      policy_loss += log_policy * td_error
      value_loss += tf.square(td_error)

    return policy_loss, value_loss

  def setup_dsl(self, factory, config):
    class ArraySyntax(object):
      def __init__(self, getitem):
        self.getitem = getitem

      def __getitem__(self, key):
        return self.getitem(key)

    self.discount = config.discount_rate
    self.reward = ArraySyntax(
      lambda t: tf.expand_dims(factory.inputs(t).reward_input, axis=1))
    self.total_reward = ArraySyntax(lambda t: tf.tile(
        tf.expand_dims(factory.inputs(t).total_reward_input, axis=1),
        multiples=[1, config.num_bootstrap_heads]))
    self.action = ArraySyntax(
      lambda t: tf.expand_dims(factory.inputs(t).action_input, axis=1))
    self.policy_network = ArraySyntax(lambda t: factory.policy_network(t))
    self.target_network = ArraySyntax(lambda t: factory.target_network(t))

    self.bootstrap_mask = factory.global_inputs.bootstrap_mask
    self.importance_sampling = factory.global_inputs.importance_sampling
# Creates everything the agent needs inside a freshly booted local GitLab.
#
#   docker exec -i habodek-gitlab gitlab-rails runner - < local/bootstrap_gitlab.rb
#
# Prints GITLAB_BOT_USER_ID and GITLAB_TOKEN for agent/.env. Re-running it is
# safe: the user is reused and only the token is reissued.
#
# `gitlab-rails runner` takes 30-60s to boot the Rails environment, so this does
# every setup step in one pass rather than one call per step.

USERNAME = 'habodek-bot'.freeze
TOKEN_NAME = 'habodek-local'.freeze

password = SecureRandom.alphanumeric(24)

user = User.find_by(username: USERNAME)
created = user.nil?

if created
  # Not `User.new(...).save!` — as of GitLab 17 a user also needs a personal
  # namespace and an organization, and building those by hand is exactly what
  # this service is for. Without it the save fails on "Namespace can't be blank".
  params = {
    username: USERNAME,
    email: "#{USERNAME}@habodek.local",
    name: 'Habodek Reviewer',
    password: password,
    password_confirmation: password,
    skip_confirmation: true
  }
  if defined?(Organizations::Organization)
    params[:organization_id] = Organizations::Organization.default_organization&.id
  end

  # Acting as root: the service authorises against the caller, and nil is nobody.
  result = Users::CreateService.new(User.find_by(username: 'root'), params).execute
  user = result.respond_to?(:payload) ? result.payload[:user] : result
  raise "user creation failed: #{result.inspect}" if user.nil? || !user.persisted?

  # Admin so it can read every seeded project without per-project membership.
  # Fine for a throwaway local instance; the real deployment gets a plain user
  # added to each repository it reviews.
  user.update!(admin: true)
end

# Reissue rather than reuse: the plaintext of an existing token is not
# recoverable, so a stale one in agent/.env cannot be verified or repaired.
user.personal_access_tokens.where(name: TOKEN_NAME).delete_all

token_value = "glpat-#{SecureRandom.alphanumeric(20)}"
pat = user.personal_access_tokens.build(
  name: TOKEN_NAME,
  scopes: %w[api],
  expires_at: 364.days.from_now
)
pat.set_token(token_value)
pat.save!

# Without this GitLab refuses to call a webhook on the host and fails silently.
settings = ApplicationSetting.current
settings.update!(
  allow_local_requests_from_web_hooks_and_services: true,
  # 13 projects arrive at once from the seeder.
  throttle_authenticated_api_enabled: false
)

puts '--- habodek bootstrap ---'
puts "GITLAB_BOT_USER_ID=#{user.id}"
puts "GITLAB_TOKEN=#{token_value}"
puts "BOT_PASSWORD=#{created ? password : '(unchanged, user already existed)'}"
puts "LOCAL_WEBHOOKS_ALLOWED=#{settings.allow_local_requests_from_web_hooks_and_services}"
puts '--- end ---'

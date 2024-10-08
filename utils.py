"""
General utils: 
- Estimation of the upper and lower bounds for the mutual information of the compressed variables
- Functions for calculating similarities for InfoNCE
- The Bhattacharyya calculation for quantifying the distinguishability of representations in the learned compression schemes
"""
import tensorflow as tf
import numpy as np

def estimate_mi_sandwich_bounds(encoder, 
  dataset, eval_batch_size=1024, num_eval_batches=8):
  """Computes the upper and lower bounds of mutual information transmitted by an encoder, given the dataset.
  
  With X and U the random variables representing the data and the compressed
  messages, respectively, we assume the conditional distribution output by the
  encoder p(u|x) is a diagonal Gaussian in latent space and parameterized by the
  center and log variance values.  As the conditional distribution is known, we 
  can use the InfoNCE lower and "leave one out" upper bounds from Poole et al.
  (2019).  See the manuscript and/or colab notebook for analysis of the bounds
  up to several bits of transmitted information.

  Args:
    encoder: TF model that produces an encoding U given input data 
      (no assumptions about architecture of the model or form of the data).
    dataset: tensorflow.data.Dataset that yields data of a single feature.
    evaluation_batch_size: The number of data points to use for each batch when
      estimating the upper and lower bounds.  Increasing this parameter yields 
      tighter bounds on the mutual information.
    number_evaluation_batches: The number of batches over which to average the 
      upper and lower bounds.  Increasing this parameter reduces the uncertainty
      of the bounds.
  Returns:
    Lower and upper bound estimates for the communication channel represented by
    the encoder.
  """
  @tf.function
  def compute_batch(batch_data):
    mus, logvars = tf.split(encoder(batch_data), 2, axis=-1) 
    # We desire extra numerical precision below; cast to float64
    mus = tf.cast(mus, tf.float64)  
    logvars = tf.cast(logvars, tf.float64)
    embedding_dimension = tf.shape(mus)[-1]
    stddevs = tf.exp(logvars/2.)
    sampled_u_values = tf.random.normal(mus.shape, mean=mus, 
                                        stddev=stddevs, dtype=tf.float64)
    # Expand dimensions to broadcast and compute the pairwise distances between
    # the sampled points and the centers of the conditional distributions
    sampled_u_values = tf.reshape(sampled_u_values, 
     [eval_batch_size, 1, embedding_dimension])
    mus = tf.reshape(mus, [1, eval_batch_size, embedding_dimension])
    distances_ui_muj = sampled_u_values - mus
    
    normalized_distances_ui_muj = distances_ui_muj / tf.reshape(stddevs, [1, eval_batch_size, embedding_dimension])
    p_ui_cond_xj = tf.exp(-tf.reduce_sum(normalized_distances_ui_muj**2, axis=-1)/2. - \
      tf.reshape(tf.reduce_sum(logvars, axis=-1), [1, eval_batch_size])/2.)
    normalization_factor = (2.*np.pi)**(tf.cast(embedding_dimension, tf.float64)/2.)
    p_ui_cond_xj = p_ui_cond_xj / normalization_factor
    # InfoNCE (lower bound) is the diagonal terms over their rows, averaged
    p_ui_cond_xi = tf.linalg.diag_part(p_ui_cond_xj)
    avg_pui_cond_xj = tf.reduce_mean(p_ui_cond_xj, axis=1)
    infonce_lower = tf.reduce_mean(tf.math.log(p_ui_cond_xi/tf.reduce_mean(p_ui_cond_xj, axis=1)))
    # "Leave one out" (upper bound) is the same but without the diagonal term in the denom
    p_ui_cond_xj *= (1. - tf.eye(eval_batch_size, dtype=tf.float64))
    loo_upper = tf.reduce_mean(tf.math.log(p_ui_cond_xi/tf.reduce_mean(p_ui_cond_xj, axis=1)))
    return infonce_lower, loo_upper

  # number_evaluation_batches*evaluation_batch_size can be larger than the dataset 
  # We gain from re-sampling u even if we have seen the data point x before 
  bound_estimates = []
  for batch_data in dataset.repeat().shuffle(eval_batch_size*10).batch(eval_batch_size).take(num_eval_batches):
    bound_estimates.append(compute_batch(batch_data)) 
    
  return np.mean(np.stack(bound_estimates, 0), 0)

@tf.function
def pairwise_l2_distance(pts1, pts2):
  """Computes squared L2 distances between each element of each set of points.
  
  Args:
    pts1: [N, d] tensor of points.
    pts2: [M, d] tensor of points.
  Returns:
    distance_matrix: [N, M] tensor of distances.
  """
  norm1 = tf.reduce_sum(tf.square(pts1), axis=-1, keepdims=True)
  norm2 = tf.reduce_sum(tf.square(pts2), axis=-1)
  norm2 = tf.expand_dims(norm2, -2)
  distance_matrix = tf.maximum(
      norm1 + norm2 - 2.0 * tf.matmul(pts1, pts2, transpose_b=True), 0.0)
  return distance_matrix


@tf.function
def pairwise_l1_distance(pts1, pts2):
  """Computes L1 distances between each element of each set of points.
  
  Args:
    pts1: [N, d] tensor of points.
    pts2: [M, d] tensor of points.
  Returns:
    distance_matrix: [N, M] tensor of distances.
  """
  stack_size2 = pts2.shape[0]
  pts1_tiled = tf.tile(tf.expand_dims(pts1, 1), [1, stack_size2, 1])
  distance_matrix = tf.reduce_sum(tf.abs(pts1_tiled-pts2), -1)
  return distance_matrix


@tf.function
def pairwise_linf_distance(pts1, pts2):
  """Computes Chebyshev distances between each element of each set of points.
  
  The Chebyshev/chessboard distance is the L_infinity distance between two
  points, the maximum difference between any of their dimensions.
  Args:
    pts1: [N, d] tensor of points.
    pts2: [M, d] tensor of points.
  Returns:
    distance_matrix: [N, M] tensor of distances.
  """
  stack_size2 = pts2.shape[0]
  pts1_tiled = tf.tile(tf.expand_dims(pts1, 1), [1, stack_size2, 1])
  distance_matrix = tf.reduce_max(tf.abs(pts1_tiled-pts2), -1)
  return distance_matrix


def get_scaled_similarity(embeddings1,
                          embeddings2,
                          similarity_type,
                          temperature):
  """Returns matrix of similarities between two sets of embeddings.
  
  Similarity is a scalar relating two embeddings, such that a more similar pair
  of embeddings has a higher value of similarity than a less similar pair.  This
  is intentionally vague to emphasize the freedom in defining measures of
  similarity. For the similarities defined, the distance-related ones range from
  -inf to 0 and cosine similarity ranges from -1 to 1.
  Args:
    embeddings1: [N, d] float tensor of embeddings.
    embeddings2: [M, d] float tensor of embeddings.
    similarity_type: String with the method of computing similarity between
      embeddings. Implemented:
        l2sq -- Squared L2 (Euclidean) distance
        l2 -- L2 (Euclidean) distance
        l1 -- L1 (Manhattan) distance
        linf -- L_inf (Chebyshev) distance
        cosine -- Cosine similarity, the inner product of the normalized vectors
    temperature: Float value which divides all similarity values, setting a
      scale for the similarity values.  Should be positive.
  Returns:  
    distance_matrix: [N, M] tensor of similarities.
  Raises:
    ValueError: If the similarity type is not recognized.
  """
  eps = 1e-9
  if similarity_type == 'l2sq':
    similarity = -1.0 * pairwise_l2_distance(embeddings1, embeddings2)
  elif similarity_type == 'l2':
    # Add a small value eps in the square root so that the gradient is always
    # with respect to a nonzero value.
    similarity = -1.0 * tf.sqrt(
        pairwise_l2_distance(embeddings1, embeddings2) + eps)
  elif similarity_type == 'l1':
    similarity = -1.0 * pairwise_l1_distance(embeddings1, embeddings2)
  elif similarity_type == 'linf':
    similarity = -1.0 * pairwise_linf_distance(embeddings1, embeddings2)
  elif similarity_type == 'cosine':
    embeddings1, _ = tf.linalg.normalize(embeddings1, ord=2, axis=-1)
    embeddings2, _ = tf.linalg.normalize(embeddings2, ord=2, axis=-1)
    similarity = tf.matmul(embeddings1, embeddings2, transpose_b=True)
  else:
    raise ValueError('Similarity type not implemented: ', similarity_type)

  similarity /= temperature
  return similarity

def bhattacharyya_dist_mat(mus1, logvars1, mus2, logvars2):
  """Computes Bhattacharyya distances between multivariate Gaussians.

  Args:
    mus1: [N, d] float array of the means of the Gaussians.
    logvars1: [N, d] float array of the log variances of the Gaussians (so we're assuming diagonal 
    covariance matrices; these are the logs of the diagonal).
    mus2: [M, d] float array of the means of the Gaussians.
    logvars2: [M, d] float array of the log variances of the Gaussians.
  Returns:
    [N, M] array of distances.
  """
  N = mus1.shape[0]
  M = mus2.shape[0]
  embedding_dimension = mus1.shape[1]
  assert (mus2.shape[1] == embedding_dimension)

  ## Manually broadcast in case either M or N is 1
  mus1 = np.tile(mus1[:, np.newaxis], [1, M, 1])
  logvars1 = np.tile(logvars1[:, np.newaxis], [1, M, 1])
  mus2 = np.tile(mus2[np.newaxis], [N, 1, 1])
  logvars2 = np.tile(logvars2[np.newaxis], [N, 1, 1])
  difference_mus = mus1 - mus2  # [N, M, embedding_dimension]; we want [N, M, embedding_dimension, 1]
  difference_mus = difference_mus[..., np.newaxis]
  difference_mus_T = np.transpose(difference_mus, [0, 1, 3, 2])

  sigma_diag = 0.5 * (np.exp(logvars1) + np.exp(logvars2))  ## [N, M, embedding_dimension], but we want a diag mat [N, M, embedding_dimension, embedding_dimension]
  sigma_mat = np.apply_along_axis(np.diag, -1, sigma_diag)
  sigma_mat_inv = np.apply_along_axis(np.diag, -1, 1./sigma_diag)

  determinant_sigma = np.prod(sigma_diag, axis=-1)
  determinant_sigma1 = np.exp(np.sum(logvars1, axis=-1))
  determinant_sigma2 = np.exp(np.sum(logvars2, axis=-1))
  term1 = 0.125 * (difference_mus_T @ sigma_mat_inv @ difference_mus).reshape([N, M])
  term2 = 0.5 * np.log(determinant_sigma / np.sqrt(determinant_sigma1 * determinant_sigma2))
  return term1+term2

def kl_divergence_mat(mus1, logvars1, mus2, logvars2):
  N = mus1.shape[0]
  M = mus2.shape[0]
  embedding_dimension = mus1.shape[1]
  assert (mus2.shape[1] == embedding_dimension)

  mus1 = np.tile(mus1[:, np.newaxis], [1, M, 1])
  logvars1 = np.tile(logvars1[:, np.newaxis], [1, M, 1])
  mus2 = np.tile(mus2[np.newaxis], [N, 1, 1])
  logvars2 = np.tile(logvars2[np.newaxis], [N, 1, 1])
  difference_mus = mus2 - mus1  # [N, M, embedding_dimension]; we want [N, M, embedding_dimension, 1]
  difference_mus = difference_mus[..., np.newaxis]
  difference_mus_T = np.transpose(difference_mus, [0, 1, 3, 2])

  sigma1_diag = np.exp(logvars1)  
  sigma2_diag = np.exp(logvars2)  
  sigma2_inv_diag = np.exp(-logvars2)  

  sigma2_mat_inv = np.apply_along_axis(np.diag, -1, 1./sigma2_diag)

  term1 = np.sum(sigma2_inv_diag * sigma1_diag, axis=-1)  # [N, M]

  term2 = (difference_mus_T @ sigma2_mat_inv @ difference_mus).reshape([N, M])

  log_determinant_sigma1 = np.sum(logvars1, axis=-1)  ## [N, M]
  log_determinant_sigma2 = np.sum(logvars2, axis=-1)  ## [N, M]

  kl_mat = log_determinant_sigma2 - log_determinant_sigma1 - embedding_dimension

  kl_mat += term1
  kl_mat += term2

  kl_mat *= 0.5

  return kl_mat

def compute_entropy_bits(probability_arr):
  return -np.sum(probability_arr*np.log2(np.where(probability_arr>0, probability_arr, 1)))
  
def entropy_rate_scaling_ansatz(N, h_inf, gamma, c):
  ## The ansatz from Schurmann and Grassberger 1995 for the
  ## scaling of entropy rate with sequence length N
  return h_inf + np.log2(N) / (N ** gamma) / np.abs(c)

def compute_entropy(seq):
  _, counts = np.unique(seq, return_counts=True)
  counts = counts / np.sum(counts)
  ent = -np.sum(counts * np.log2(counts))
  return ent
